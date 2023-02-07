from bees import profiler as p
'Implementation of an image service that uses Glance as the backend.'
import copy
import inspect
import itertools
import os
import random
import re
import stat
import sys
import time
import urllib.parse as urlparse
import cryptography
from cursive import certificate_utils
from cursive import exception as cursive_exception
from cursive import signature_utils
import glanceclient
from glanceclient.common import utils as glance_utils
import glanceclient.exc
from glanceclient.v2 import schemas
from keystoneauth1 import loading as ks_loading
from oslo_log import log as logging
from oslo_serialization import jsonutils
from oslo_utils import excutils
from oslo_utils import timeutils
import nova.conf
from nova import exception
from nova import objects
from nova.objects import fields
from nova import profiler
from nova import service_auth
from nova import utils
LOG = logging.getLogger(__name__)
CONF = nova.conf.CONF
_SESSION = None

@p.trace('_session_and_auth')
def _session_and_auth(context):
    global _SESSION
    if not _SESSION:
        _SESSION = ks_loading.load_session_from_conf_options(CONF, nova.conf.glance.glance_group.name)
    auth = service_auth.get_auth_plugin(context)
    return (_SESSION, auth)

@p.trace('_glanceclient_from_endpoint')
def _glanceclient_from_endpoint(context, endpoint, version):
    (sess, auth) = _session_and_auth(context)
    return glanceclient.Client(version, session=sess, auth=auth, endpoint_override=endpoint, global_request_id=context.global_id)

@p.trace('generate_glance_url')
def generate_glance_url(context):
    """Return a random glance url from the api servers we know about."""
    return next(get_api_servers(context))

@p.trace('_endpoint_from_image_ref')
def _endpoint_from_image_ref(image_href):
    """Return the image_ref and guessed endpoint from an image url.

    :param image_href: href of an image
    :returns: a tuple of the form (image_id, endpoint_url)
    """
    parts = image_href.split('/')
    image_id = parts[-1]
    endpoint = '/'.join(parts[:-3])
    return (image_id, endpoint)

@p.trace('generate_identity_headers')
def generate_identity_headers(context, status='Confirmed'):
    return {'X-Auth-Token': getattr(context, 'auth_token', None), 'X-User-Id': getattr(context, 'user_id', None), 'X-Tenant-Id': getattr(context, 'project_id', None), 'X-Roles': ','.join(getattr(context, 'roles', [])), 'X-Identity-Status': status}

@p.trace('get_api_servers')
def get_api_servers(context):
    """Shuffle a list of service endpoints and return an iterator that will
    cycle through the list, looping around to the beginning if necessary.
    """
    if CONF.glance.api_servers:
        api_servers = CONF.glance.api_servers
        random.shuffle(api_servers)
    else:
        (sess, auth) = _session_and_auth(context)
        ksa_adap = utils.get_ksa_adapter(nova.conf.glance.DEFAULT_SERVICE_TYPE, ksa_auth=auth, ksa_session=sess, min_version='2.0', max_version='2.latest')
        endpoint = utils.get_endpoint(ksa_adap)
        if endpoint:
            endpoint = re.sub('/v\\d+(\\.\\d+)?/?$', '/', endpoint)
        api_servers = [endpoint]
    return itertools.cycle(api_servers)

@p.trace_cls('GlanceClientWrapper')
class GlanceClientWrapper(object):
    """Glance client wrapper class that implements retries."""

    def __init__(self, context=None, endpoint=None):
        version = 2
        if endpoint is not None:
            self.client = self._create_static_client(context, endpoint, version)
        else:
            self.client = None
        self.api_servers = None

    def _create_static_client(self, context, endpoint, version):
        """Create a client that we'll use for every call."""
        self.api_server = str(endpoint)
        return _glanceclient_from_endpoint(context, endpoint, version)

    def _create_onetime_client(self, context, version):
        """Create a client that will be used for one call."""
        if self.api_servers is None:
            self.api_servers = get_api_servers(context)
        self.api_server = next(self.api_servers)
        return _glanceclient_from_endpoint(context, self.api_server, version)

    def call(self, context, version, method, controller=None, args=None, kwargs=None):
        """Call a glance client method.  If we get a connection error,
        retry the request according to CONF.glance.num_retries.

        :param context: RequestContext to use
        :param version: Numeric version of the *Glance API* to use
        :param method: string method name to execute on the glanceclient
        :param controller: optional string name of the client controller to
                           use. Default (None) is to use the 'images'
                           controller
        :param args: optional iterable of arguments to pass to the
                     glanceclient method, splatted as positional args
        :param kwargs: optional dict of arguments to pass to the glanceclient,
                       splatted into named arguments
        """
        args = args or []
        kwargs = kwargs or {}
        retry_excs = (glanceclient.exc.ServiceUnavailable, glanceclient.exc.InvalidEndpoint, glanceclient.exc.CommunicationError)
        num_attempts = 1 + CONF.glance.num_retries
        controller_name = controller or 'images'
        for attempt in range(1, num_attempts + 1):
            client = self.client or self._create_onetime_client(context, version)
            try:
                controller = getattr(client, controller_name)
                result = getattr(controller, method)(*args, **kwargs)
                if inspect.isgenerator(result):
                    return list(result)
                return result
            except retry_excs as e:
                if attempt < num_attempts:
                    extra = 'retrying'
                else:
                    extra = 'done trying'
                LOG.exception("Error contacting glance server '%(server)s' for '%(method)s', %(extra)s.", {'server': self.api_server, 'method': method, 'extra': extra})
                if attempt == num_attempts:
                    raise exception.GlanceConnectionFailed(server=str(self.api_server), reason=str(e))
                time.sleep(1)

@p.trace_cls('GlanceImageServiceV2')
class GlanceImageServiceV2(object):
    """Provides storage and retrieval of disk image objects within Glance."""

    def __init__(self, client=None):
        self._client = client or GlanceClientWrapper()
        self._download_handlers = {}
        if CONF.glance.enable_rbd_download:
            self._download_handlers['rbd'] = self.rbd_download

    def rbd_download(self, context, url_parts, dst_path, metadata=None):
        """Use an explicit rbd call to download an image.

        :param context: The `nova.context.RequestContext` object for the
                        request
        :param url_parts: Parts of URL pointing to the image location
        :param dst_path: Filepath to transfer the image file to.
        :param metadata: Image location metadata (currently unused)
        """
        from nova.storage import rbd_utils
        try:
            url_path = str(urlparse.unquote(url_parts.path))
            (cluster_uuid, pool_name, image_uuid, snapshot_name) = url_path.split('/')
        except ValueError as e:
            msg = f'Invalid RBD URL format: {e}'
            LOG.error(msg)
            raise nova.exception.InvalidParameterValue(msg)
        rbd_driver = rbd_utils.RBDDriver(user=CONF.glance.rbd_user, pool=CONF.glance.rbd_pool, ceph_conf=CONF.glance.rbd_ceph_conf, connect_timeout=CONF.glance.rbd_connect_timeout)
        try:
            LOG.debug('Attempting to export RBD image: [pool_name: %s] [image_uuid: %s] [snapshot_name: %s] [dst_path: %s]', pool_name, image_uuid, snapshot_name, dst_path)
            rbd_driver.export_image(dst_path, image_uuid, snapshot_name, pool_name)
        except Exception as e:
            LOG.error('Error during RBD image export: %s', e)
            raise nova.exception.CouldNotFetchImage(image_id=image_uuid)

    def show(self, context, image_id, include_locations=False, show_deleted=True):
        """Returns a dict with image data for the given opaque image id.

        :param context: The context object to pass to image client
        :param image_id: The UUID of the image
        :param include_locations: (Optional) include locations in the returned
                                  dict of information if the image service API
                                  supports it. If the image service API does
                                  not support the locations attribute, it will
                                  still be included in the returned dict, as an
                                  empty list.
        :param show_deleted: (Optional) show the image even the status of
                             image is deleted.
        """
        try:
            image = self._client.call(context, 2, 'get', args=(image_id,))
        except Exception:
            _reraise_translated_image_exception(image_id)
        if not show_deleted and getattr(image, 'deleted', False):
            raise exception.ImageNotFound(image_id=image_id)
        if not _is_image_available(context, image):
            raise exception.ImageNotFound(image_id=image_id)
        image = _translate_from_glance(image, include_locations=include_locations)
        if include_locations:
            locations = image.get('locations', None) or []
            du = image.get('direct_url', None)
            if du:
                locations.append({'url': du, 'metadata': {}})
            image['locations'] = locations
        return image

    def _get_transfer_method(self, scheme):
        """Returns a transfer method for scheme, or None."""
        try:
            return self._download_handlers[scheme]
        except KeyError:
            return None

    def detail(self, context, **kwargs):
        """Calls out to Glance for a list of detailed image information."""
        params = _extract_query_params_v2(kwargs)
        try:
            images = self._client.call(context, 2, 'list', kwargs=params)
        except Exception:
            _reraise_translated_exception()
        _images = []
        for image in images:
            if _is_image_available(context, image):
                _images.append(_translate_from_glance(image))
        return _images

    @staticmethod
    def _safe_fsync(fh):
        """Performs os.fsync on a filehandle only if it is supported.

        fsync on a pipe, FIFO, or socket raises OSError with EINVAL.  This
        method discovers whether the target filehandle is one of these types
        and only performs fsync if it isn't.

        :param fh: Open filehandle (not a path or fileno) to maybe fsync.
        """
        fileno = fh.fileno()
        mode = os.fstat(fileno).st_mode
        if not any((check(mode) for check in (stat.S_ISFIFO, stat.S_ISSOCK))):
            os.fsync(fileno)

    def download(self, context, image_id, data=None, dst_path=None, trusted_certs=None):
        """Calls out to Glance for data and writes data."""
        if self._download_handlers and dst_path is not None:
            image = self.show(context, image_id, include_locations=True)
            for entry in image.get('locations', []):
                loc_url = entry['url']
                loc_meta = entry['metadata']
                o = urlparse.urlparse(loc_url)
                xfer_method = self._get_transfer_method(o.scheme)
                if xfer_method:
                    try:
                        xfer_method(context, o, dst_path, loc_meta)
                        LOG.info('Successfully transferred using %s', o.scheme)
                        with open(dst_path, 'rb') as fh:
                            downloaded_length = os.path.getsize(dst_path)
                            image_chunks = glance_utils.IterableWithLength(fh, downloaded_length)
                            self._verify_and_write(context, image_id, trusted_certs, image_chunks, None, None)
                        return
                    except Exception:
                        LOG.exception('Download image error')
        try:
            image_chunks = self._client.call(context, 2, 'data', args=(image_id,))
        except Exception:
            _reraise_translated_image_exception(image_id)
        if image_chunks.wrapped is None:
            raise exception.ImageUnacceptable(image_id=image_id, reason='Image has no associated data')
        return self._verify_and_write(context, image_id, trusted_certs, image_chunks, data, dst_path)

    def _verify_and_write(self, context, image_id, trusted_certs, image_chunks, data, dst_path):
        """Perform image signature verification and save the image file if needed.

        This function writes the content of the image_chunks iterator either to
        a file object provided by the data parameter or to a filepath provided
        by dst_path parameter. If none of them are provided then no data will
        be written out but instead image_chunks iterator is returned.

        :param image_id: The UUID of the image
        :param trusted_certs: A 'nova.objects.trusted_certs.TrustedCerts'
                              object with a list of trusted image certificate
                              IDs.
        :param image_chunks An iterator pointing to the image data
        :param data: File object to use when writing the image.
            If passed as None and dst_path is provided, new file is opened.
        :param dst_path: Filepath to transfer the image file to.
        :returns an iterable with image data, or nothing. Iterable is returned
            only when data param is None and dst_path is not provided (assuming
            the caller wants to process the data by itself).

        """
        close_file = False
        if data is None and dst_path:
            data = open(dst_path, 'wb')
            close_file = True
        write_image = True
        if data is None:
            write_image = False
        verifier = self._get_verifier(context, image_id, trusted_certs)
        if verifier is None and write_image is False:
            if data is None:
                return image_chunks
            else:
                return
        try:
            for chunk in image_chunks:
                if verifier:
                    verifier.update(chunk)
                if write_image:
                    data.write(chunk)
            if verifier:
                verifier.verify()
                LOG.info('Image signature verification succeeded for image %s', image_id)
        except cryptography.exceptions.InvalidSignature:
            if write_image:
                data.truncate(0)
            with excutils.save_and_reraise_exception():
                LOG.error('Image signature verification failed for image %s', image_id)
        except Exception as ex:
            if write_image:
                with excutils.save_and_reraise_exception():
                    LOG.error('Error writing to %(path)s: %(exception)s', {'path': dst_path, 'exception': ex})
            else:
                with excutils.save_and_reraise_exception():
                    LOG.error('Error during image verification: %s', ex)
        finally:
            if close_file:
                data.flush()
                self._safe_fsync(data)
                data.close()
        if data is None:
            return image_chunks

    def _get_verifier(self, context, image_id, trusted_certs):
        verifier = None
        if not trusted_certs and CONF.glance.enable_certificate_validation and CONF.glance.default_trusted_certificate_ids:
            trusted_certs = objects.TrustedCerts(ids=CONF.glance.default_trusted_certificate_ids)
        if trusted_certs or CONF.glance.verify_glance_signatures:
            image_meta_dict = self.show(context, image_id, include_locations=False)
            image_meta = objects.ImageMeta.from_dict(image_meta_dict)
            img_signature = image_meta.properties.get('img_signature')
            img_sig_hash_method = image_meta.properties.get('img_signature_hash_method')
            img_sig_cert_uuid = image_meta.properties.get('img_signature_certificate_uuid')
            img_sig_key_type = image_meta.properties.get('img_signature_key_type')
            try:
                verifier = signature_utils.get_verifier(context=context, img_signature_certificate_uuid=img_sig_cert_uuid, img_signature_hash_method=img_sig_hash_method, img_signature=img_signature, img_signature_key_type=img_sig_key_type)
            except cursive_exception.SignatureVerificationError:
                with excutils.save_and_reraise_exception():
                    LOG.error('Image signature verification failed for image: %s', image_id)
            if trusted_certs:
                _verify_certs(context, img_sig_cert_uuid, trusted_certs)
            elif CONF.glance.enable_certificate_validation:
                msg = 'Image signature certificate validation enabled, but no trusted certificate IDs were provided. Unable to validate the certificate used to verify the image signature.'
                LOG.warning(msg)
                raise exception.CertificateValidationFailed(msg)
            else:
                LOG.debug('Certificate validation was not performed. A list of trusted image certificate IDs must be provided in order to validate an image certificate.')
        return verifier

    def create(self, context, image_meta, data=None):
        """Store the image data and return the new image object."""
        force_activate = data is None and image_meta.get('size') == 0
        sharing_member_id = image_meta.get('properties', {}).pop('instance_owner', None)
        sent_service_image_meta = _translate_to_glance(image_meta)
        try:
            image = self._create_v2(context, sent_service_image_meta, data, force_activate, sharing_member_id=sharing_member_id)
        except glanceclient.exc.HTTPException:
            _reraise_translated_exception()
        return _translate_from_glance(image)

    def _add_location(self, context, image_id, location):
        try:
            return self._client.call(context, 2, 'add_location', args=(image_id, location, {}))
        except glanceclient.exc.HTTPBadRequest:
            _reraise_translated_exception()

    def _add_image_member(self, context, image_id, member_id):
        """Grant access to another project that does not own the image

        :param context: nova auth RequestContext where context.project_id is
            the owner of the image
        :param image_id: ID of the image on which to grant access
        :param member_id: ID of the member project to grant access to the
            image; this should not be the owner of the image
        :returns: A Member schema object of the created image member
        """
        try:
            return self._client.call(context, 2, 'create', controller='image_members', args=(image_id, member_id))
        except glanceclient.exc.HTTPBadRequest:
            _reraise_translated_exception()

    def _upload_data(self, context, image_id, data):
        utils.tpool_execute(self._client.call, context, 2, 'upload', args=(image_id, data))
        return self._client.call(context, 2, 'get', args=(image_id,))

    def _get_image_create_disk_format_default(self, context):
        """Gets an acceptable default image disk_format based on the schema.
        """
        preferred_disk_formats = (fields.DiskFormat.QCOW2, fields.DiskFormat.VHD, fields.DiskFormat.VMDK, fields.DiskFormat.RAW)
        image_schema = self._client.call(context, 2, 'get', args=('image',), controller='schemas')
        disk_format_schema = image_schema.raw()['properties'].get('disk_format') if image_schema else {}
        if disk_format_schema and 'enum' in disk_format_schema:
            supported_disk_formats = disk_format_schema['enum']
            for preferred_format in preferred_disk_formats:
                if preferred_format in supported_disk_formats:
                    return preferred_format
            LOG.debug('Unable to find a preferred disk_format for image creation with the Image Service v2 API. Using: %s', supported_disk_formats[0])
            return supported_disk_formats[0]
        LOG.warning('Unable to determine disk_format schema from the Image Service v2 API. Defaulting to %(preferred_disk_format)s.', {'preferred_disk_format': preferred_disk_formats[0]})
        return preferred_disk_formats[0]

    def _create_v2(self, context, sent_service_image_meta, data=None, force_activate=False, sharing_member_id=None):
        if force_activate:
            data = ''
            if 'disk_format' not in sent_service_image_meta:
                sent_service_image_meta['disk_format'] = self._get_image_create_disk_format_default(context)
            if 'container_format' not in sent_service_image_meta:
                sent_service_image_meta['container_format'] = 'bare'
        location = sent_service_image_meta.pop('location', None)
        image = self._client.call(context, 2, 'create', kwargs=sent_service_image_meta)
        image_id = image['id']
        if location:
            image = self._add_location(context, image_id, location)
        if sharing_member_id:
            LOG.debug('Adding access for member %s to image %s', sharing_member_id, image_id)
            self._add_image_member(context, image_id, sharing_member_id)
        if data is not None:
            image = self._upload_data(context, image_id, data)
        return image

    def update(self, context, image_id, image_meta, data=None, purge_props=True):
        """Modify the given image with the new data."""
        sent_service_image_meta = _translate_to_glance(image_meta)
        sent_service_image_meta.pop('id', None)
        sent_service_image_meta['image_id'] = image_id
        try:
            if purge_props:
                all_props = set(self.show(context, image_id)['properties'].keys())
                props_to_update = set(image_meta.get('properties', {}).keys())
                remove_props = list(all_props - props_to_update)
                sent_service_image_meta['remove_props'] = remove_props
            image = self._update_v2(context, sent_service_image_meta, data)
        except Exception:
            _reraise_translated_image_exception(image_id)
        return _translate_from_glance(image)

    def _update_v2(self, context, sent_service_image_meta, data=None):
        location = sent_service_image_meta.pop('location', None)
        image_id = sent_service_image_meta['image_id']
        image = self._client.call(context, 2, 'update', kwargs=sent_service_image_meta)
        if location:
            image = self._add_location(context, image_id, location)
        if data is not None:
            image = self._upload_data(context, image_id, data)
        return image

    def delete(self, context, image_id):
        """Delete the given image.

        :raises: ImageNotFound if the image does not exist.
        :raises: NotAuthorized if the user is not an owner.
        :raises: ImageNotAuthorized if the user is not authorized.
        :raises: ImageDeleteConflict if the image is conflicted to delete.

        """
        try:
            self._client.call(context, 2, 'delete', args=(image_id,))
        except glanceclient.exc.NotFound:
            raise exception.ImageNotFound(image_id=image_id)
        except glanceclient.exc.HTTPForbidden:
            raise exception.ImageNotAuthorized(image_id=image_id)
        except glanceclient.exc.HTTPConflict as exc:
            raise exception.ImageDeleteConflict(reason=str(exc))
        return True

    def image_import_copy(self, context, image_id, stores):
        """Copy an image to another store using image_import.

        This triggers the Glance image_import API with an opinionated
        method of 'copy-image' to a list of stores. This will initiate
        a copy of the image from one of the existing stores to the
        stores provided.

        :param context: The RequestContext
        :param image_id: The image to copy
        :param stores: A list of stores to copy the image to

        :raises: ImageNotFound if the image does not exist.
        :raises: ImageNotAuthorized if the user is not permitted to
                 import/copy this image
        :raises: ImageImportImpossible if the image cannot be imported
                 for workflow reasons (not active, etc)
        :raises: ImageBadRequest if the image is already in the requested
                 store (which may be a race)
        """
        try:
            self._client.call(context, 2, 'image_import', args=(image_id,), kwargs={'method': 'copy-image', 'stores': stores})
        except glanceclient.exc.NotFound:
            raise exception.ImageNotFound(image_id=image_id)
        except glanceclient.exc.HTTPForbidden:
            raise exception.ImageNotAuthorized(image_id=image_id)
        except glanceclient.exc.HTTPConflict as exc:
            raise exception.ImageImportImpossible(image_id=image_id, reason=str(exc))
        except glanceclient.exc.HTTPBadRequest as exc:
            raise exception.ImageBadRequest(image_id=image_id, response=str(exc))

@p.trace('_extract_query_params_v2')
def _extract_query_params_v2(params):
    _params = {}
    accepted_params = ('filters', 'marker', 'limit', 'page_size', 'sort_key', 'sort_dir')
    for param in accepted_params:
        if params.get(param):
            _params[param] = params.get(param)
    _params.setdefault('filters', {})
    _params['filters'].setdefault('is_public', 'none')
    filters = _params['filters']
    new_filters = {}
    for filter_ in filters:
        if filter_.startswith('property-'):
            new_filters[filter_.lstrip('property-')] = filters[filter_]
        elif filter_ == 'changes-since':
            updated_at = 'gte:' + filters['changes-since']
            new_filters['updated_at'] = updated_at
        elif filter_ == 'is_public':
            is_public = filters['is_public']
            if is_public.lower() in ('true', '1'):
                new_filters['visibility'] = 'public'
            elif is_public.lower() in ('false', '0'):
                new_filters['visibility'] = 'private'
        else:
            new_filters[filter_] = filters[filter_]
    _params['filters'] = new_filters
    return _params

@p.trace('_is_image_available')
def _is_image_available(context, image):
    """Check image availability.

    This check is needed in case Nova and Glance are deployed
    without authentication turned on.
    """
    if hasattr(context, 'auth_token') and context.auth_token:
        return True

    def _is_image_public(image):
        if hasattr(image, 'visibility'):
            return str(image.visibility).lower() == 'public'
        else:
            return image.is_public
    if context.is_admin or _is_image_public(image):
        return True
    properties = image.properties
    if context.project_id and 'owner_id' in properties:
        return str(properties['owner_id']) == str(context.project_id)
    if context.project_id and 'project_id' in properties:
        return str(properties['project_id']) == str(context.project_id)
    try:
        user_id = properties['user_id']
    except KeyError:
        return False
    return str(user_id) == str(context.user_id)

@p.trace('_translate_to_glance')
def _translate_to_glance(image_meta):
    image_meta = _convert_to_string(image_meta)
    image_meta = _remove_read_only(image_meta)
    image_meta = _convert_to_v2(image_meta)
    return image_meta

@p.trace('_convert_to_v2')
def _convert_to_v2(image_meta):
    output = {}
    for (name, value) in image_meta.items():
        if name == 'properties':
            for (prop_name, prop_value) in value.items():
                if prop_name in ('kernel_id', 'ramdisk_id') and prop_value is not None and (prop_value.strip().lower() in ('none', '')):
                    continue
                elif prop_value is None or isinstance(prop_value, str):
                    output[prop_name] = prop_value
                else:
                    output[prop_name] = str(prop_value)
        elif name in ('min_ram', 'min_disk'):
            output[name] = int(value)
        elif name == 'is_public':
            output['visibility'] = 'public' if value else 'private'
        elif name in ('size', 'deleted'):
            continue
        else:
            output[name] = value
    return output

@p.trace('_translate_from_glance')
def _translate_from_glance(image, include_locations=False):
    image_meta = _extract_attributes_v2(image, include_locations=include_locations)
    image_meta = _convert_timestamps_to_datetimes(image_meta)
    image_meta = _convert_from_string(image_meta)
    return image_meta

@p.trace('_convert_timestamps_to_datetimes')
def _convert_timestamps_to_datetimes(image_meta):
    """Returns image with timestamp fields converted to datetime objects."""
    for attr in ['created_at', 'updated_at', 'deleted_at']:
        if image_meta.get(attr):
            image_meta[attr] = timeutils.parse_isotime(image_meta[attr])
    return image_meta

@p.trace('_json_loads')
def _json_loads(properties, attr):
    prop = properties[attr]
    if isinstance(prop, str):
        properties[attr] = jsonutils.loads(prop)

@p.trace('_json_dumps')
def _json_dumps(properties, attr):
    prop = properties[attr]
    if not isinstance(prop, str):
        properties[attr] = jsonutils.dumps(prop)
_CONVERT_PROPS = ('block_device_mapping', 'mappings')

@p.trace('_convert')
def _convert(method, metadata):
    metadata = copy.deepcopy(metadata)
    properties = metadata.get('properties')
    if properties:
        for attr in _CONVERT_PROPS:
            if attr in properties:
                method(properties, attr)
    return metadata

@p.trace('_convert_from_string')
def _convert_from_string(metadata):
    return _convert(_json_loads, metadata)

@p.trace('_convert_to_string')
def _convert_to_string(metadata):
    return _convert(_json_dumps, metadata)

@p.trace('_extract_attributes')
def _extract_attributes(image, include_locations=False):
    IMAGE_ATTRIBUTES = ['size', 'disk_format', 'owner', 'container_format', 'status', 'id', 'name', 'created_at', 'updated_at', 'deleted', 'deleted_at', 'checksum', 'min_disk', 'min_ram', 'is_public', 'direct_url', 'locations']
    queued = getattr(image, 'status') == 'queued'
    queued_exclude_attrs = ['disk_format', 'container_format']
    include_locations_attrs = ['direct_url', 'locations']
    output = {}
    for attr in IMAGE_ATTRIBUTES:
        if attr == 'deleted_at' and (not output['deleted']):
            output[attr] = None
        elif attr == 'checksum' and output['status'] != 'active':
            output[attr] = None
        elif attr == 'name':
            output[attr] = getattr(image, attr, None)
        elif queued and attr in queued_exclude_attrs:
            output[attr] = getattr(image, attr, None)
        elif attr in include_locations_attrs:
            if include_locations:
                output[attr] = getattr(image, attr, None)
        elif attr == 'size':
            output[attr] = getattr(image, attr, 0) or 0
        else:
            output[attr] = getattr(image, attr, None)
    output['properties'] = getattr(image, 'properties', {})
    return output

@p.trace('_extract_attributes_v2')
def _extract_attributes_v2(image, include_locations=False):
    include_locations_attrs = ['direct_url', 'locations']
    omit_attrs = ['self', 'schema', 'protected', 'virtual_size', 'file', 'tags']
    raw_schema = image.schema
    schema = schemas.Schema(raw_schema)
    output = {'properties': {}, 'deleted': False, 'deleted_at': None, 'disk_format': None, 'container_format': None, 'name': None, 'checksum': None}
    for (name, value) in image.items():
        if name in omit_attrs or (name in include_locations_attrs and (not include_locations)):
            continue
        elif name == 'visibility':
            output['is_public'] = value == 'public'
        elif name == 'size' and value is None:
            output['size'] = 0
        elif schema.is_base_property(name):
            output[name] = value
        else:
            output['properties'][name] = value
    return output

@p.trace('_remove_read_only')
def _remove_read_only(image_meta):
    IMAGE_ATTRIBUTES = ['status', 'updated_at', 'created_at', 'deleted_at']
    output = copy.deepcopy(image_meta)
    for attr in IMAGE_ATTRIBUTES:
        if attr in output:
            del output[attr]
    return output

@p.trace('_reraise_translated_image_exception')
def _reraise_translated_image_exception(image_id):
    """Transform the exception for the image but keep its traceback intact."""
    (exc_type, exc_value, exc_trace) = sys.exc_info()
    new_exc = _translate_image_exception(image_id, exc_value)
    raise new_exc.with_traceback(exc_trace)

@p.trace('_reraise_translated_exception')
def _reraise_translated_exception():
    """Transform the exception but keep its traceback intact."""
    (exc_type, exc_value, exc_trace) = sys.exc_info()
    new_exc = _translate_plain_exception(exc_value)
    raise new_exc.with_traceback(exc_trace)

@p.trace('_translate_image_exception')
def _translate_image_exception(image_id, exc_value):
    if isinstance(exc_value, (glanceclient.exc.Forbidden, glanceclient.exc.Unauthorized)):
        return exception.ImageNotAuthorized(image_id=image_id)
    if isinstance(exc_value, glanceclient.exc.NotFound):
        return exception.ImageNotFound(image_id=image_id)
    if isinstance(exc_value, glanceclient.exc.BadRequest):
        return exception.ImageBadRequest(image_id=image_id, response=str(exc_value))
    if isinstance(exc_value, glanceclient.exc.HTTPOverLimit):
        return exception.ImageQuotaExceeded(image_id=image_id)
    return exc_value

@p.trace('_translate_plain_exception')
def _translate_plain_exception(exc_value):
    if isinstance(exc_value, (glanceclient.exc.Forbidden, glanceclient.exc.Unauthorized)):
        return exception.Forbidden(str(exc_value))
    if isinstance(exc_value, glanceclient.exc.NotFound):
        return exception.NotFound(str(exc_value))
    if isinstance(exc_value, glanceclient.exc.BadRequest):
        return exception.Invalid(str(exc_value))
    return exc_value

@p.trace('_verify_certs')
def _verify_certs(context, img_sig_cert_uuid, trusted_certs):
    try:
        certificate_utils.verify_certificate(context=context, certificate_uuid=img_sig_cert_uuid, trusted_certificate_uuids=trusted_certs.ids)
        LOG.debug('Image signature certificate validation succeeded for certificate: %s', img_sig_cert_uuid)
    except cursive_exception.SignatureVerificationError as e:
        LOG.warning('Image signature certificate validation failed for certificate: %s', img_sig_cert_uuid)
        raise exception.CertificateValidationFailed(cert_uuid=img_sig_cert_uuid, reason=str(e))

@p.trace('get_remote_image_service')
def get_remote_image_service(context, image_href):
    """Create an image_service and parse the id from the given image_href.

    The image_href param can be an href of the form
    'http://example.com:9292/v1/images/b8b2c6f7-7345-4e2f-afa2-eedaba9cbbe3',
    or just an id such as 'b8b2c6f7-7345-4e2f-afa2-eedaba9cbbe3'. If the
    image_href is a standalone id, then the default image service is returned.

    :param image_href: href that describes the location of an image
    :returns: a tuple of the form (image_service, image_id)

    """
    if '/' not in str(image_href):
        image_service = get_default_image_service()
        return (image_service, image_href)
    try:
        (image_id, endpoint) = _endpoint_from_image_ref(image_href)
        glance_client = GlanceClientWrapper(context=context, endpoint=endpoint)
    except ValueError:
        raise exception.InvalidImageRef(image_href=image_href)
    image_service = GlanceImageServiceV2(client=glance_client)
    return (image_service, image_id)

@p.trace('get_default_image_service')
def get_default_image_service():
    return GlanceImageServiceV2()

@p.trace_cls('UpdateGlanceImage')
class UpdateGlanceImage(object):

    def __init__(self, context, image_id, metadata, stream):
        self.context = context
        self.image_id = image_id
        self.metadata = metadata
        self.image_stream = stream

    def start(self):
        (image_service, image_id) = get_remote_image_service(self.context, self.image_id)
        image_service.update(self.context, image_id, self.metadata, self.image_stream, purge_props=False)

@p.trace_cls('API')
@profiler.trace_cls('nova_image')
class API(object):
    """API for interacting with the image service."""

    def _get_session_and_image_id(self, context, id_or_uri):
        """Returns a tuple of (session, image_id). If the supplied `id_or_uri`
        is an image ID, then the default client session will be returned
        for the context's user, along with the image ID. If the supplied
        `id_or_uri` parameter is a URI, then a client session connecting to
        the URI's image service endpoint will be returned along with a
        parsed image ID from that URI.

        :param context: The `nova.context.Context` object for the request
        :param id_or_uri: A UUID identifier or an image URI to look up image
                          information for.
        """
        return get_remote_image_service(context, id_or_uri)

    def _get_session(self, _context):
        """Returns a client session that can be used to query for image
        information.

        :param _context: The `nova.context.Context` object for the request
        """
        return get_default_image_service()

    @staticmethod
    def generate_image_url(image_ref, context):
        """Generate an image URL from an image_ref.

        :param image_ref: The image ref to generate URL
        :param context: The `nova.context.Context` object for the request
        """
        return '%s/images/%s' % (next(get_api_servers(context)), image_ref)

    def get_all(self, context, **kwargs):
        """Retrieves all information records about all disk images available
        to show to the requesting user. If the requesting user is an admin,
        all images in an ACTIVE status are returned. If the requesting user
        is not an admin, the all public images and all private images that
        are owned by the requesting user in the ACTIVE status are returned.

        :param context: The `nova.context.Context` object for the request
        :param kwargs: A dictionary of filter and pagination values that
                       may be passed to the underlying image info driver.
        """
        session = self._get_session(context)
        return session.detail(context, **kwargs)

    def get(self, context, id_or_uri, include_locations=False, show_deleted=True):
        """Retrieves the information record for a single disk image. If the
        supplied identifier parameter is a UUID, the default driver will
        be used to return information about the image. If the supplied
        identifier is a URI, then the driver that matches that URI endpoint
        will be used to query for image information.

        :param context: The `nova.context.Context` object for the request
        :param id_or_uri: A UUID identifier or an image URI to look up image
                          information for.
        :param include_locations: (Optional) include locations in the returned
                                  dict of information if the image service API
                                  supports it. If the image service API does
                                  not support the locations attribute, it will
                                  still be included in the returned dict, as an
                                  empty list.
        :param show_deleted: (Optional) show the image even the status of
                             image is deleted.
        """
        (session, image_id) = self._get_session_and_image_id(context, id_or_uri)
        return session.show(context, image_id, include_locations=include_locations, show_deleted=show_deleted)

    def create(self, context, image_info, data=None):
        """Creates a new image record, optionally passing the image bits to
        backend storage.

        :param context: The `nova.context.Context` object for the request
        :param image_info: A dict of information about the image that is
                           passed to the image registry.
        :param data: Optional file handle or bytestream iterator that is
                     passed to backend storage.
        """
        session = self._get_session(context)
        return session.create(context, image_info, data=data)

    def update(self, context, id_or_uri, image_info, data=None, purge_props=False):
        """Update the information about an image, optionally along with a file
        handle or bytestream iterator for image bits. If the optional file
        handle for updated image bits is supplied, the image may not have
        already uploaded bits for the image.

        :param context: The `nova.context.Context` object for the request
        :param id_or_uri: A UUID identifier or an image URI to look up image
                          information for.
        :param image_info: A dict of information about the image that is
                           passed to the image registry.
        :param data: Optional file handle or bytestream iterator that is
                     passed to backend storage.
        :param purge_props: Optional, defaults to False. If set, the backend
                            image registry will clear all image properties
                            and replace them the image properties supplied
                            in the image_info dictionary's 'properties'
                            collection.
        """
        (session, image_id) = self._get_session_and_image_id(context, id_or_uri)
        return session.update(context, image_id, image_info, data=data, purge_props=purge_props)

    def delete(self, context, id_or_uri):
        """Delete the information about an image and mark the image bits for
        deletion.

        :param context: The `nova.context.Context` object for the request
        :param id_or_uri: A UUID identifier or an image URI to look up image
                          information for.
        """
        (session, image_id) = self._get_session_and_image_id(context, id_or_uri)
        return session.delete(context, image_id)

    def download(self, context, id_or_uri, data=None, dest_path=None, trusted_certs=None):
        """Transfer image bits from Glance or a known source location to the
        supplied destination filepath.

        :param context: The `nova.context.RequestContext` object for the
                        request
        :param id_or_uri: A UUID identifier or an image URI to look up image
                          information for.
        :param data: A file object to use in downloading image data.
        :param dest_path: Filepath to transfer image bits to.
        :param trusted_certs: A 'nova.objects.trusted_certs.TrustedCerts'
                              object with a list of trusted image certificate
                              IDs.

        Note that because of the poor design of the
        `glance.ImageService.download` method, the function returns different
        things depending on what arguments are passed to it. If a data argument
        is supplied but no dest_path is specified (not currently done by any
        caller) then None is returned from the method. If the data argument is
        not specified but a destination path *is* specified, then a writeable
        file handle to the destination path is constructed in the method and
        the image bits written to that file, and again, None is returned from
        the method. If no data argument is supplied and no dest_path argument
        is supplied (VMWare virt driver), then the method returns an iterator
        to the image bits that the caller uses to write to wherever location it
        wants. Finally, if the allow_direct_url_schemes CONF option is set to
        something, then the nova.image.download modules are used to attempt to
        do an SCP copy of the image bits from a file location to the dest_path
        and None is returned after retrying one or more download locations
        (libvirt and Hyper-V virt drivers through nova.virt.images.fetch).

        I think the above points to just how hacky/wacky all of this code is,
        and the reason it needs to be cleaned up and standardized across the
        virt driver callers.
        """
        (session, image_id) = self._get_session_and_image_id(context, id_or_uri)
        return session.download(context, image_id, data=data, dst_path=dest_path, trusted_certs=trusted_certs)

    def copy_image_to_store(self, context, image_id, store):
        """Initiate a store-to-store copy in glance.

        :param context: The RequestContext.
        :param image_id: The image to copy.
        :param store: The glance store to target the copy.
        """
        (session, image_id) = self._get_session_and_image_id(context, image_id)
        return session.image_import_copy(context, image_id, [store])