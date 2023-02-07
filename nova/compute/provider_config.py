from bees import profiler as p
import glob
import jsonschema
import logging
import microversion_parse
import os
import yaml
import os_resource_classes
import os_traits
from nova import exception as nova_exc
from nova.i18n import _
LOG = logging.getLogger(__name__)
SUPPORTED_SCHEMA_VERSIONS = {1: {0}}
SCHEMA_V1 = {'type': 'object', 'properties': {'__source_file': {'not': {}}, 'meta': {'type': 'object', 'properties': {'schema_version': {'type': 'string', 'pattern': '^1.([0-9]|[1-9][0-9]+)$'}}, 'required': ['schema_version'], 'additionalProperties': True}, 'providers': {'type': 'array', 'items': {'type': 'object', 'properties': {'identification': {'$ref': '#/$defs/providerIdentification'}, 'inventories': {'$ref': '#/$defs/providerInventories'}, 'traits': {'$ref': '#/$defs/providerTraits'}}, 'required': ['identification'], 'additionalProperties': True}}}, 'required': ['meta'], 'additionalProperties': True, '$defs': {'providerIdentification': {'type': 'object', 'properties': {'uuid': {'oneOf': [{'type': 'string', 'pattern': '^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$'}, {'type': 'string', 'const': '$COMPUTE_NODE'}]}, 'name': {'type': 'string', 'minLength': 1, 'maxLength': 200}}, 'minProperties': 1, 'maxProperties': 1, 'additionalProperties': False}, 'providerInventories': {'type': 'object', 'properties': {'additional': {'type': 'array', 'items': {'patternProperties': {'^[A-Z0-9_]{1,255}$': {'type': 'object', 'properties': {'total': {'type': 'integer'}, 'reserved': {'type': 'integer'}, 'min_unit': {'type': 'integer'}, 'max_unit': {'type': 'integer'}, 'step_size': {'type': 'integer'}, 'allocation_ratio': {'type': 'number'}}, 'required': ['total'], 'additionalProperties': False}}, 'additionalProperties': False}}}, 'additionalProperties': True}, 'providerTraits': {'type': 'object', 'properties': {'additional': {'type': 'array', 'items': {'type': 'string', 'pattern': '^[A-Z0-9_]{1,255}$'}}}, 'additionalProperties': True}}}

@p.trace('_load_yaml_file')
def _load_yaml_file(path):
    """Loads and parses a provider.yaml config file into a dict.

    :param path: Path to the yaml file to load.
    :return: Dict representing the yaml file requested.
    :raise: ProviderConfigException if the path provided cannot be read
            or the file is not valid yaml.
    """
    try:
        with open(path) as open_file:
            try:
                return yaml.safe_load(open_file)
            except yaml.YAMLError as ex:
                message = _('Unable to load yaml file: %s ') % ex
                if hasattr(ex, 'problem_mark'):
                    pos = ex.problem_mark
                    message += _('File: %s ') % open_file.name
                    message += _('Error position: (%s:%s)') % (pos.line + 1, pos.column + 1)
                raise nova_exc.ProviderConfigException(error=message)
    except OSError:
        message = _('Unable to read yaml config file: %s') % path
        raise nova_exc.ProviderConfigException(error=message)

@p.trace('_validate_provider_config')
def _validate_provider_config(config, provider_config_path):
    """Accepts a schema-verified provider config in the form of a dict and
     performs additional checks for format and required keys.

    :param config: Dict containing a provider config file
    :param provider_config_path: Path to the provider config, used for logging
    :return: List of valid providers
    :raise nova.exception.ProviderConfigException: If provider id is missing,
        or a resource class or trait name is invalid.
    """

    def _validate_traits(provider):
        additional_traits = set(provider.get('traits', {}).get('additional', []))
        trait_conflicts = [trait for trait in additional_traits if not os_traits.is_custom(trait)]
        if trait_conflicts:
            message = _('Invalid traits, only custom traits are allowed: %s') % sorted(trait_conflicts)
            raise nova_exc.ProviderConfigException(error=message)
        return additional_traits

    def _validate_rc(provider):
        additional_inventories = provider.get('inventories', {}).get('additional', [])
        all_inventory_conflicts = []
        for inventory in additional_inventories:
            inventory_conflicts = [rc for rc in inventory if not os_resource_classes.is_custom(rc)]
            all_inventory_conflicts += inventory_conflicts
        if all_inventory_conflicts:
            message = _('Invalid resource class, only custom resource classes are allowed: %s') % ', '.join(sorted(all_inventory_conflicts))
            raise nova_exc.ProviderConfigException(error=message)
        return additional_inventories
    valid_providers = []
    for provider in config.get('providers', []):
        pid = provider['identification']
        provider_id = pid.get('name') or pid.get('uuid')
        additional_traits = _validate_traits(provider)
        additional_inventories = _validate_rc(provider)
        if not additional_traits and (not additional_inventories):
            message = 'Provider %(provider_id)s defined in %(provider_config_path)s has no additional inventories or traits and will be ignored.' % {'provider_id': provider_id, 'provider_config_path': provider_config_path}
            LOG.warning(message)
        else:
            valid_providers.append(provider)
    return valid_providers

@p.trace('_parse_provider_yaml')
def _parse_provider_yaml(path):
    """Loads schema, parses a provider.yaml file and validates the content.

    :param path: File system path to the file to parse.
    :return: dict representing the contents of the file.
    :raise ProviderConfigException: If the specified file does
        not validate against the schema, the schema version is not supported,
        or if unable to read configuration or schema files.
    """
    yaml_file = _load_yaml_file(path)
    try:
        schema_version = microversion_parse.parse_version_string(yaml_file['meta']['schema_version'])
    except (KeyError, TypeError):
        message = _('Unable to detect schema version: %s') % yaml_file
        raise nova_exc.ProviderConfigException(error=message)
    if schema_version.major not in SUPPORTED_SCHEMA_VERSIONS:
        message = _('Unsupported schema major version: %d') % schema_version.major
        raise nova_exc.ProviderConfigException(error=message)
    if schema_version.minor not in SUPPORTED_SCHEMA_VERSIONS[schema_version.major]:
        message = 'Provider config file [%(path)s] is at schema version %(schema_version)s. Nova supports the major version, but not the minor. Some fields may be ignored.' % {'path': path, 'schema_version': schema_version}
        LOG.warning(message)
    try:
        jsonschema.validate(yaml_file, SCHEMA_V1)
    except jsonschema.exceptions.ValidationError as e:
        message = _('The provider config file %(path)s did not pass validation for schema version %(schema_version)s: %(reason)s') % {'path': path, 'schema_version': schema_version, 'reason': e}
        raise nova_exc.ProviderConfigException(error=message)
    return yaml_file

@p.trace('get_provider_configs')
def get_provider_configs(provider_config_dir):
    """Gathers files in the provided path and calls the parser for each file
    and merges them into a list while checking for a number of possible
    conflicts.

    :param provider_config_dir: Path to a directory containing provider config
        files to be loaded.
    :raise nova.exception.ProviderConfigException: If unable to read provider
        config directory or if one of a number of validation checks fail:
        - Unknown, unsupported, or missing schema major version.
        - Unknown, unsupported, or missing resource provider identification.
        - A specific resource provider is identified twice with the same
          method. If the same provider identified by *different* methods,
          such conflict will be detected in a later stage.
        - A resource class or trait name is invalid or not custom.
        - A general schema validation error occurs (required fields,
          types, etc).
    :return: A dict of dicts keyed by uuid_or_name with the parsed and
    validated contents of all files in the provided dir. Each value in the dict
    will include the source file name the value of the __source_file key.
    """
    provider_configs = {}
    provider_config_paths = glob.glob(os.path.join(provider_config_dir, '*.yaml'))
    provider_config_paths.sort()
    if not provider_config_paths:
        message = 'No provider configs found in %s. If files are present, ensure the Nova process has access.'
        LOG.info(message, provider_config_dir)
        return provider_configs
    for provider_config_path in provider_config_paths:
        provider_config = _parse_provider_yaml(provider_config_path)
        for provider in _validate_provider_config(provider_config, provider_config_path):
            provider['__source_file'] = os.path.basename(provider_config_path)
            pid = provider['identification']
            uuid_or_name = pid.get('uuid') or pid.get('name')
            if uuid_or_name in provider_configs:
                raise nova_exc.ProviderConfigException(error=_('Provider %(provider_id)s has multiple definitions in source file(s): %(source_files)s.') % {'provider_id': uuid_or_name, 'source_files': sorted({provider_configs[uuid_or_name]['__source_file'], provider_config_path})})
            provider_configs[uuid_or_name] = provider
    return provider_configs