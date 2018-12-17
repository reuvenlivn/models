# Copyright 2017 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Runs a ResNet model on the ImageNet dataset."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import time

from absl import app as absl_app
from absl import flags
import numpy as np
import tensorflow as tf  # pylint: disable=g-bad-import-order

from official.resnet import imagenet_main
from official.resnet import imagenet_preprocessing
from official.resnet import resnet_run_loop
from official.resnet.keras import keras_resnet_model
from official.resnet.keras import resnet_model_tpu
from official.utils.flags import core as flags_core
from official.utils.logs import logger
from official.utils.misc import distribution_utils
from tensorflow.python.keras.optimizer_v2 import gradient_descent as gradient_descent_v2


class TimeHistory(tf.keras.callbacks.Callback):
  """Callback for Keras models."""

  def __init__(self, batch_size):
    """Callback for Keras models.

    Args:
      batch_size: Total batch size.

    """
    self._batch_size = batch_size
    super(TimeHistory, self).__init__()

  def on_train_begin(self, logs=None):
    self.epoch_times_secs = []
    self.batch_times_secs = []
    self.record_batch = True

  def on_epoch_begin(self, epoch, logs=None):
    self.epoch_time_start = time.time()

  def on_epoch_end(self, epoch, logs=None):
    self.epoch_times_secs.append(time.time() - self.epoch_time_start)

  def on_batch_begin(self, batch, logs=None):
    if self.record_batch:
      self.batch_time_start = time.time()
      self.record_batch = False

  def on_batch_end(self, batch, logs=None):
    n = 100
    if batch % n == 0:
      last_n_batches = time.time() - self.batch_time_start
      examples_per_second = (self._batch_size * n) / last_n_batches
      self.batch_times_secs.append(last_n_batches)
      self.record_batch = True
      # TODO(anjalisridhar): add timestamp as well.
      if batch != 0:
        tf.logging.info("BenchmarkMetric: {'num_batches':%d, 'time_taken': %f,"
                        "'images_per_second': %f}" %
                        (batch, last_n_batches, examples_per_second))


LR_SCHEDULE = [    # (multiplier, epoch to start) tuples
    (1.0, 5), (0.1, 30), (0.01, 60), (0.001, 80)
]
BASE_LEARNING_RATE = 0.1  # This matches Jing's version.

def learning_rate_schedule(current_epoch, current_batch, batches_per_epoch, batch_size):
  """Handles linear scaling rule, gradual warmup, and LR decay.

  The learning rate starts at 0, then it increases linearly per step.
  After 5 epochs we reach the base learning rate (scaled to account
    for batch size).
  After 30, 60 and 80 epochs the learning rate is divided by 10.
  After 90 epochs training stops and the LR is set to 0. This ensures
    that we train for exactly 90 epochs for reproducibility.

  Args:
    current_epoch: integer, current epoch indexed from 0.
    current_batch: integer, current batch in the current epoch, indexed from 0.

  Returns:
    Adjusted learning rate.
  """
  initial_learning_rate = BASE_LEARNING_RATE * batch_size / 256
  epoch = current_epoch + float(current_batch) / batches_per_epoch
  warmup_lr_multiplier, warmup_end_epoch = LR_SCHEDULE[0]
  if epoch < warmup_end_epoch:
    # Learning rate increases linearly per step.
    return initial_learning_rate * warmup_lr_multiplier * epoch / warmup_end_epoch
  for mult, start_epoch in LR_SCHEDULE:
    if epoch >= start_epoch:
      learning_rate = initial_learning_rate * mult
    else:
      break
  return learning_rate


class LearningRateBatchScheduler(tf.keras.callbacks.Callback):
  """Callback to update learning rate on every batch (not epoch boundaries).

  N.B. Only support Keras optimizers, not TF optimizers.

  Args:
      schedule: a function that takes an epoch index and a batch index as input
          (both integer, indexed from 0) and returns a new learning rate as
          output (float).
  """

  def __init__(self, schedule, batch_size, num_images):
    super(LearningRateBatchScheduler, self).__init__()
    self.schedule = schedule
    self.batches_per_epoch = num_images / batch_size
    self.batch_size = batch_size
    self.epochs = -1
    self.prev_lr = -1

  def on_epoch_begin(self, epoch, logs=None):
    #if not hasattr(self.model.optimizer, 'learning_rate'):
    #  raise ValueError('Optimizer must have a "learning_rate" attribute.')
    self.epochs += 1

  def on_batch_begin(self, batch, logs=None):
    lr = self.schedule(self.epochs, batch, self.batches_per_epoch, self.batch_size)
    if not isinstance(lr, (float, np.float32, np.float64)):
      raise ValueError('The output of the "schedule" function should be float.')
    if lr != self.prev_lr:
      tf.keras.backend.set_value(self.model.optimizer.learning_rate, lr)
      self.prev_lr = lr
      tf.logging.debug('Epoch %05d Batch %05d: LearningRateBatchScheduler change '
                   'learning rate to %s.', self.epochs, batch, lr)



def parse_record_keras(raw_record, is_training, dtype):
  """Adjust the shape of label."""
  image, label = imagenet_main.parse_record(raw_record, is_training, dtype)
  # Subtract one so that labels are in [0, 1000), and cast to float32 for
  # Keras model.
  label = tf.cast(tf.cast(tf.reshape(label, shape=[1]), dtype=tf.int32) - 1,
                  dtype=tf.float32)
  return image, label


def run_imagenet_with_keras(flags_obj):
  """Run ResNet ImageNet training and eval loop using native Keras APIs.

  Args:
    flags_obj: An object containing parsed flag values.

  Raises:
    ValueError: If fp16 is passed as it is not currently supported.
  """
  if flags_obj.enable_eager:
    tf.enable_eager_execution()

  dtype = flags_core.get_tf_dtype(flags_obj)
  if dtype == 'fp16':
    raise ValueError('dtype fp16 is not supported in Keras. Use the default '
                     'value(fp32).')

  per_device_batch_size = distribution_utils.per_device_batch_size(
      flags_obj.batch_size, flags_core.get_num_gpus(flags_obj))

  # pylint: disable=protected-access
  if flags_obj.use_synthetic_data:
    synth_input_fn = resnet_run_loop.get_synth_input_fn(
        imagenet_main._DEFAULT_IMAGE_SIZE, imagenet_main._DEFAULT_IMAGE_SIZE,
        imagenet_main._NUM_CHANNELS, imagenet_main._NUM_CLASSES,
        dtype=flags_core.get_tf_dtype(flags_obj))
    train_input_dataset = synth_input_fn(
        is_training=True,
        data_dir=None,
        batch_size=per_device_batch_size,
        height=imagenet_main._DEFAULT_IMAGE_SIZE,
        width=imagenet_main._DEFAULT_IMAGE_SIZE,
        num_channels=imagenet_main._NUM_CHANNELS,
        num_classes=imagenet_main._NUM_CLASSES,
        dtype=dtype)
    eval_input_dataset = synth_input_fn(
        is_training=False,
        data_dir=None,
        batch_size=per_device_batch_size,
        height=imagenet_main._DEFAULT_IMAGE_SIZE,
        width=imagenet_main._DEFAULT_IMAGE_SIZE,
        num_channels=imagenet_main._NUM_CHANNELS,
        num_classes=imagenet_main._NUM_CLASSES,
        dtype=dtype)
  # pylint: enable=protected-access

  else:
    train_input_dataset = imagenet_main.input_fn(
          True,
          flags_obj.data_dir,
          batch_size=per_device_batch_size,
          num_epochs=flags_obj.train_epochs,
          parse_record_fn=parse_record_keras)

    eval_input_dataset = imagenet_main.input_fn(
          False,
          flags_obj.data_dir,
          batch_size=per_device_batch_size,
          num_epochs=flags_obj.train_epochs,
          parse_record_fn=parse_record_keras)


  # Use Keras ResNet50 applications model and native keras APIs
  # initialize RMSprop optimizer
  # TODO(anjalisridhar): Move to using MomentumOptimizer.
  # opt = tf.train.GradientDescentOptimizer(learning_rate=0.0001)
  # I am setting an initial LR of 0.001 since this will be reset
  # at the beginning of the training loop.
  opt = gradient_descent_v2.SGD(learning_rate=0.1, momentum=0.9)

  # TF Optimizer:
  # learning_rate = BASE_LEARNING_RATE * flags_obj.batch_size / 256
  # opt = tf.train.MomentumOptimizer(learning_rate=learning_rate, momentum=0.9)


  strategy = distribution_utils.get_distribution_strategy(
      num_gpus=flags_obj.num_gpus)

  if flags_obj.use_tpu_model:
    model = resnet_model_tpu.ResNet50(num_classes=imagenet_main._NUM_CLASSES)
  else:
    model = keras_resnet_model.ResNet50(classes=imagenet_main._NUM_CLASSES,
                                        weights=None)

  print(">>>>>>>>>>>>>>> Model input: ", model.input)
  print(">>>>>>>>>>>>>>> Model output: ", model.output)
  print(">>>>>>>>>>>>>>> data: ", train_input_dataset.output)

  loss = 'sparse_categorical_crossentropy'
  accuracy = 'sparse_categorical_accuracy'

  model.compile(loss=loss,
                optimizer=opt,
                metrics=[accuracy],
                distribute=strategy)

  steps_per_epoch = imagenet_main._NUM_IMAGES['train'] // flags_obj.batch_size

  time_callback = TimeHistory(flags_obj.batch_size)

  tesorboard_callback = tf.keras.callbacks.TensorBoard(
    log_dir=flags_obj.model_dir)
    #update_freq="batch")  # Add this if want per batch logging.

  lr_callback = LearningRateBatchScheduler(
    learning_rate_schedule,
    batch_size=flags_obj.batch_size,
    num_images=imagenet_main._NUM_IMAGES['train'])

  num_eval_steps = (imagenet_main._NUM_IMAGES['validation'] //
                  flags_obj.batch_size)

  model.fit(train_input_dataset,
            epochs=flags_obj.train_epochs,
            steps_per_epoch=steps_per_epoch,
            callbacks=[
              time_callback,
              lr_callback,
              tesorboard_callback
            ],
            validation_steps=num_eval_steps,
            validation_data=eval_input_dataset,
            verbose=1)

  eval_output = model.evaluate(eval_input_dataset,
                               steps=num_eval_steps,
                               verbose=1)
  print('Test loss:', eval_output[0])

def define_keras_imagenet_flags():
  flags.DEFINE_boolean(name='enable_eager', default=False, help='Enable eager?')


def main(_):
  with logger.benchmark_context(flags.FLAGS):
    run_imagenet_with_keras(flags.FLAGS)


if __name__ == '__main__':
  tf.logging.set_verbosity(tf.logging.INFO)
  define_keras_imagenet_flags()
  imagenet_main.define_imagenet_flags()
  flags.DEFINE_boolean(name='use_tpu_model', default=False, help='Use resnet model from tpu.')
  absl_app.run(main)