# conding -*- utf-8 -*-
from tensorflow.python.lib.io.tf_record import TFRecordWriter
from tensorflow.python.lib.io.tf_record import tf_record_iterator
from tensorflow.python.lib.io.tf_record import TFRecordOptions, TFRecordCompressionType
tf_writer = TFRecordWriter('rawtemp.tfrecords')  # TFRecordOptions(TFRecordCompressionType.GZIP)
tf_writer.write(b'the 1st test string')
tf_writer.flush()
tf_writer.write(b'the 2nd test string')
tf_writer.flush()
tf_writer.write(b'the 3rd test string')
tf_writer.flush()
tf_writer.write(b'the 4th test string')
tf_writer.flush()
tf_writer.write(b'the 5th test string')
tf_writer.flush()
tf_writer.write(b'the 6th test string')
tf_writer.flush()
tf_writer.write(b'the 7th test string')
tf_writer.flush()
tf_writer.write(b'the 8th test string')
tf_writer.flush()
tf_writer.write(b'the 9th test string')
tf_writer.flush()
tf_writer.write(b'the th test string')
tf_writer.flush()
tf_writer.close()

# for record in tf_record_iterator('append_output.tfrecords',TFRecordOptions(TFRecordCompressionType.NONE)):   # TFRecordOptions(TFRecordCompressionType.GZIP)
#     print(record)