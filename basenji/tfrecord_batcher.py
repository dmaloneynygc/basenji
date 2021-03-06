# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     https://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# =========================================================================

import pdb

import tensorflow as tf

from basenji import dna_io
from basenji import ops


def shift_sequence(seq, shift_amount, pad_value):
  """Shift a sequence left or right by shift_amount.
  Args:
    seq: a [batch_size, sequence_length, sequence_depth] sequence to shift
    shift_amount: the signed amount to shift (tf.int32 or int)
    pad_value: value to fill the padding (primitive or scalar tf.Tensor)
  """
  if seq.shape.ndims != 3:
    raise ValueError('input sequence should be rank 3')
  input_shape = seq.shape

  pad = pad_value * tf.ones_like(seq[:, 0:tf.abs(shift_amount), :])

  def _shift_right(_seq):
    sliced_seq = _seq[:, shift_amount:, :]
    return tf.concat([pad, sliced_seq], axis=1)

  def _shift_left(_seq):
    sliced_seq = _seq[:, :shift_amount, :]
    return tf.concat([sliced_seq, pad], axis=1)

  output = tf.cond(
      tf.greater(shift_amount, 0), lambda: _shift_right(seq),
      lambda: _shift_left(seq))
  output.set_shape(input_shape)
  return output

# TODO(dbelanger) change inputs to be (features, labels) like for Estimator.
def rc_data_augmentation(dataset):
  """Apply reverse complement to seq and flip label/na along the time axis.

  Args:
    dataset: dict with keys 'sequence,' 'label,' and 'na.'
  Returns
    transformed_dataset: augmented data
    process_predictions_fn: callable to be applied to predictions
      such that they are directly comparable to the input dataset['label']
      rather than transformed_dataset['label']. Here, it flips the prediction
      along the time axis.
  """
  seq, label, na = [dataset[k] for k in ['sequence', 'label', 'na']]

  do_flip = tf.random_uniform(shape=[]) > 0.5
  seq, label, na = tf.cond(do_flip, lambda: ops.reverse_complement_transform(seq, label, na),
                           lambda: (seq, label, na))

  def process_predictions_fn(predictions):
    return tf.cond(do_flip, lambda: tf.reverse(predictions, axis=[1]),
                               lambda: predictions)

  transformed_dataset = {'sequence': seq, 'label': label, 'na': na}
  return transformed_dataset, process_predictions_fn


def shift_sequence_augmentation(seq, shift_augment_offsets, pad_value):
  """Shift seq by a random amount. Pad to maintain the input size.

  Args:
    seq: input sequence of size [batch_size, length, depth]
    shift_augment_offsets: list of int offsets to sample from. If `None` or
      `[]`, then only "shift" by 0 (the identity).
    pad_value: value to fill the padding with.
  Returns:
    shifted and padded sequence of size [batch_size, length, depth]
  """
  # The value of the parameter shift_augment_offsets are the set of things to
  # _augment_ the original data with, and we want to, in addition to including
  # those augmentations, actually include the original data.
  if shift_augment_offsets:
    total_set_of_shifts = [0] + shift_augment_offsets
  else:
    total_set_of_shifts = [0]

  shift_index = tf.random_uniform(
      shape=[], minval=0, maxval=len(total_set_of_shifts), dtype=tf.int64)
  shift_value = tf.gather(tf.constant(total_set_of_shifts), shift_index)

  seq = tf.cond(
      tf.not_equal(shift_value, 0),
      lambda: shift_sequence(seq, shift_value, pad_value), lambda: seq)

  return seq


def apply_data_augmentation(input_ops, label_ops, augment_with_complement,
                            shift_augment_offsets):
  """Apply data augmentation to input and label ops.
  Args:
    input_ops: dict containing input Tensors.
    label_ops: dict containing label Tensors.
    augment_with_complement: whether to do reverse complement augmentation.
    shift_augment_offsets: offsets used for doing shift-based augmentation.
      Can be `None` or `[]` to indicate no shift-augmentation.

  Returns:
    transformed_inputs: inputs with augmentation applied.
    transformed_labels: labels transformed in accordance with the augmentation.
    process_predictions_fn: callable to be applied to predictions
      such that they are directly comparable to the label_ops
      rather than transformed_labels.
  """
  data_ops = {}
  data_ops.update(input_ops)
  data_ops.update(label_ops)

  augmented_data_ops, process_predictions_fn = data_augmentation_from_data_ops(
      data_ops, augment_with_complement, shift_augment_offsets)
  return ({
      'sequence': augmented_data_ops['sequence']
  }, {name: augmented_data_ops[name]
      for name in ['label', 'na']}, process_predictions_fn)

# TODO(dbelanger) switch to directly calling apply_data_augmentation
def data_augmentation_from_data_ops(data_ops, augment_with_complement,
                                    shift_augment_offsets):
  process_predictions_fn = None

  if augment_with_complement:
    data_ops, process_predictions_fn = rc_data_augmentation(data_ops)

  if shift_augment_offsets and len(shift_augment_offsets) > 1:
    pad_value = 0.25
    data_ops['sequence'] = shift_sequence_augmentation(
        data_ops['sequence'], shift_augment_offsets, pad_value)

  return data_ops, process_predictions_fn


def tfrecord_dataset(tfr_data_files_pattern, batch_size, seq_length, seq_depth,
                     num_targets, target_length, mode):
  """Load TFRecord format data.

  The tf.Example assumed to be ZLIB compressed with fields:
    sequence: tf.string FixedLenFeature of length seq_length * seq_depth.
    label: tf.float32 FixedLenFeature of target_length * num_targets.

  Args:
   tfr_data_file_pattern: Pattern (potentially with globs) for TFRecord
     format files. See `tf.gfile.Glob` for more information.
    batch_size: batch_size
    seq_length: length of input sequence
    seq_depth: vocabulary size of the inputs (4 for raw DNA)
    num_targets: number of targets at each target sequence location
    target_length: length of the target sequence
j   mode: a tf.estimator.ModeKeys instance

  Returns:
    A Dataset which will produce a dict with the following tensors:
      sequence: [batch_size, sequence_length, seq_depth]
      label: [batch_size, num_targets, target_length]
      na: [batch_size, num_targets]
  """
  inputs_name = 'sequence'
  targets_name = 'target'

  training_dataset = (mode == tf.estimator.ModeKeys.TRAIN)

  dataset = tf.data.Dataset.list_files(tfr_data_files_pattern)

  def file_to_records(filename):
    return tf.data.TFRecordDataset(filename, compression_type='ZLIB')

  if training_dataset:  # Shuffle, repeat, and parallelize.
    # This shuffles just the filenames, not the examples.
    dataset = dataset.shuffle(buffer_size=100000)
    dataset = dataset.repeat()
    dataset = dataset.apply(
        # Interleaving allows us to pull one element from one file,
        # and then switch to pulling from another file, without fully reading
        # the first.
        tf.contrib.data.sloppy_interleave(
            map_func=file_to_records,
            # Magic number for cycle-length chosen by trying 64 (mentioned as a
            # best practice) and then noticing a significant bump in memory
            # usage. Reducing to 10 alleviated some of the memory pressure.
            cycle_length=10))
    # Shuffle elements within a file.
    dataset = dataset.shuffle(buffer_size=150)
  else:
    # Don't parallelize, shuffle, or repeat.
    # Use flat_map as opposed to map because file_to_records produces a dataset,
    # so with (non-flat) map, we'd have a dataset of datasets.
    dataset = dataset.flat_map(file_to_records)

  if batch_size is None:
    raise ValueError('batch_size is None')
  dataset = dataset.batch(batch_size)

  def _parse(example_protos):
    features = {
        'sequence': tf.FixedLenFeature([], tf.string),
        'target': tf.FixedLenFeature([target_length * num_targets], tf.float32),
    }

    parsed_features = tf.parse_example(example_protos, features=features)

    seq = tf.decode_raw(parsed_features['sequence'], tf.uint8)
    seq = tf.reshape(seq, [batch_size, seq_length, seq_depth])
    seq = tf.cast(seq, tf.float32)
    label = tf.cast(parsed_features['target'], tf.float32)
    label = tf.reshape(label, [batch_size, target_length, num_targets])
    na = tf.zeros(label.shape[:-1], dtype=tf.bool)

    return {'sequence': seq, 'label': label, 'na': na}

  dataset = dataset.map(_parse)

  return dataset


def make_data_ops(job,
                  files_pattern,
                  mode,
                  shuffle):
  """Get an iterator over your training data.

  Args:
    job: a dictionary of parsed parameters.
      See `basenji.google.params.read_job_params` for more information.
    files_pattern: A file path pattern that has your training data. For example,
      '/cns/sandbox/home/mlbileschi/brain/basenji/data/train/*'.
    mode: a tf.estimator.ModeKeys instance.
    shuffle: whether to shuffle the examples in the dataset.
  """
  if len(tf.gfile.Glob(files_pattern)) == 0:
    raise ValueError('0 files matched files_pattern ' + files_pattern + '.')

  batcher = tfrecord_dataset(
      files_pattern,
      job['batch_size'],
      job['seq_length'],
      job['seq_depth'],
      job['num_targets'],
      job['target_length'],
      mode=mode)

  return batcher.make_one_shot_iterator().get_next()

def num_possible_augmentations(augment_with_complement, shift_augment_offsets):
  # The value of the parameter shift_augment_offsets are the set of things to
  # _augment_ the original data with, and we want to, in addition to including
  # those augmentations, actually include the original data.
  if shift_augment_offsets:
    total_set_of_shifts = [0] + shift_augment_offsets
  else:
    total_set_of_shifts = [0]

  num_augments = 2 if augment_with_complement else 1
  num_augments *= len(total_set_of_shifts)
  return num_augments

# TODO(dbelanger) Remove this functionality.
class TFRecordBatcher(object):
  """Load TFRecord format data. Many args are unused and for API-compatibility.

     Args:
       tfr_data_file_pattern: Pattern (potentially with globs) for TFRecord
         format files. See `tf.gfile.Glob` for more information.
       load_targets: whether to load targets (unused)
       seq_length: length of the input sequences
       seq_depth: vocabulary size of the inputs (4 for raw DNA)
       target_length: length of the target sequence
       num_targets: number of targets at each target sequence location
       mode: a tf.estimator.ModeKeys instance
       NAf: (unused)
       batch_size: batch_size
       pool_width: width of pooling layers (unused)
       shuffle: whether the batcher should shuffle the data
  """

  def __init__(self,
               tfr_data_file_pattern,
               load_targets,
               seq_length,
               seq_depth,
               target_length,
               num_targets,
               mode,
               NAf=None,
               batch_size=64,
               pool_width=1,
               shuffle=False):

    self.session = None

    filenames = tf.gfile.Glob(tfr_data_file_pattern)

    dataset = tfrecord_dataset(filenames, batch_size, seq_length, seq_depth,
                               num_targets, target_length, mode)

    self.iterator = dataset.make_initializable_iterator()
    self._next_element = self.iterator.get_next()

  def initialize(self, sess):
    sess.run(self.iterator.initializer)

  def next(self, rc=False, shift=0):
    try:
      d = self.session.run(self._next_element)

      Xb = d['sequence']
      Yb = d['label']
      NAb = d['na']
      Nb = Xb.shape[0]

      # reverse complement
      if rc:
        if Xb is not None:
          Xb = dna_io.hot1_augment(Xb, rc, shift)
        if Yb is not None:
          Yb = Yb[:, ::-1, :]
        if NAb is not None:
          NAb = NAb[:, ::-1]

      return Xb, Yb, NAb, Nb

    except tf.errors.OutOfRangeError:
      return None, None, None, None

  def reset(self):
    return self.initialize(self.session)
