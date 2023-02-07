from bees import profiler as p
from oslo_config import cfg
ephemeral_storage_encryption_group = cfg.OptGroup(name='ephemeral_storage_encryption', title='Ephemeral storage encryption options')
ephemeral_storage_encryption_opts = [cfg.BoolOpt('enabled', default=False, help='\nEnables/disables LVM ephemeral storage encryption.\n'), cfg.StrOpt('cipher', default='aes-xts-plain64', help='\nCipher-mode string to be used.\n\nThe cipher and mode to be used to encrypt ephemeral storage. The set of\ncipher-mode combinations available depends on kernel support. According\nto the dm-crypt documentation, the cipher is expected to be in the format:\n"<cipher>-<chainmode>-<ivmode>".\n\nPossible values:\n\n* Any crypto option listed in ``/proc/crypto``.\n'), cfg.IntOpt('key_size', default=512, min=1, help='\nEncryption key length in bits.\n\nThe bit length of the encryption key to be used to encrypt ephemeral storage.\nIn XTS mode only half of the bits are used for encryption key.\n')]

@p.trace('register_opts')
def register_opts(conf):
    conf.register_group(ephemeral_storage_encryption_group)
    conf.register_opts(ephemeral_storage_encryption_opts, group=ephemeral_storage_encryption_group)

@p.trace('list_opts')
def list_opts():
    return {ephemeral_storage_encryption_group: ephemeral_storage_encryption_opts}