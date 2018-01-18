import struct
import crcmod
# import codecs

def _default_crc32c_fn(value):
    if not _default_crc32c_fn.fn:
        _default_crc32c_fn.fn = crcmod.predefined.mkPredefinedCrcFun('crc-32c')
    return _default_crc32c_fn.fn(value)

def _masked_crc32c(value, crc32c_fn=_default_crc32c_fn):
    crc = crc32c_fn(value)
    """ Some alternative methods """
    # crc = zlib.crc32(value)
    # crc = binascii.crc32(value)
    return (((crc >> 15) | (crc << 17)) + 0xa282ead8) & 0xffffffff

def encoded_num_bytes(record):
    """Return the number of bytes consumed by a record in its encoded form."""
    # 16 = 8 (Length) + 4 (crc of length) + 4 (crc of data)
    return len(record) + 16

def write2tfrcd(filename):
    print("Start!")
    string_value1 = b"this is a test string"
    string_value2 = b"the second string"
    encoded_length1 = struct.pack('<Q', len(string_value1))
    encoded_length2 = struct.pack('<Q', len(string_value2))
    file_handle = open(filename, 'wb')
    file_handle.write(encoded_length1 + struct.pack('<I', _masked_crc32c(encoded_length1))
                          + string_value1 + struct.pack('<I', _masked_crc32c(string_value1)))
    file_handle.write(
        encoded_length2 + struct.pack('<I', _masked_crc32c(encoded_length2)) +
        string_value2 + struct.pack('<I', _masked_crc32c(string_value2)))
    file_handle.close()

def read_single_rcd(file_handle):
    # file_handle=open(filename,'rb')
    buf_length_expected = 12
    buf = file_handle.read(buf_length_expected)
    if not buf:
        return None  # EOF Reached.
    # Validate all length related payloads.
    if len(buf) != buf_length_expected:
        raise ValueError('Not a valid TFRecord. Fewer than %d bytes: %s' %
                             (buf_length_expected, buf))
    length, length_mask_expected = struct.unpack('<QI', buf)
    length_mask_actual = _masked_crc32c(buf[:8])
    if length_mask_actual != length_mask_expected:
        raise ValueError('Not a valid TFRecord. Mismatch of length mask: %s' % buf)
    # Validate all data related payloads.
    buf_length_expected = length + 4
    buf = file_handle.read(buf_length_expected)
    if len(buf) != buf_length_expected:
        raise ValueError('Not a valid TFRecord. Fewer than %d bytes: %s' %
                             (buf_length_expected, buf))
    data, data_mask_expected = struct.unpack('<%dsI' % length, buf)
    data_mask_actual = _masked_crc32c(data)
    if data_mask_actual != data_mask_expected:
            raise ValueError('Not a valid TFRecord. Mismatch of data mask: %s' % buf)  # codecs.encode(buf, 'hex')
    print(data)
    # All validation checks passed.
    return data

def read_records(file_name):
    current_offset = 0
    with open(file_name, 'rb') as file_handle:
        while True:
            file_handle.seek(current_offset)
            record = read_single_rcd(file_handle)
            if record is None:
                return  # Reached EOF
            else:
                current_offset += encoded_num_bytes(record)

if __name__=='__main__':
    _default_crc32c_fn.fn = None
    # write2tfrcd("2str_test.tfrecords")
    read_records("2str_test.tfrecords")