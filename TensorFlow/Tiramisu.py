#
# Based on:
#   The One Hundred Layers Tiramisu: Fully Convolutional DenseNets for Semantic Segmentation
#   https://arxiv.org/abs/1611.09326
#

import tensorflow as tf
from Conv2dUtilities import Conv2dUtilities

class Tiramisu:

  def __init__(
      self, number_of_preprocessing_convolution_filters, number_of_filters_for_convolution_blocks,
      number_of_convolutions_per_block, use_multiscale_output=False,
      activation_function=tf.nn.relu, use_batch_normalization=True, dropout_rate=0.2, data_format='channels_last'):

    self.number_of_preprocessing_convolution_filters = number_of_preprocessing_convolution_filters
    self.number_of_filters_for_convolution_blocks = number_of_filters_for_convolution_blocks
    self.number_of_convolutions_per_block = number_of_convolutions_per_block
    self.use_multiscale_output = use_multiscale_output
    self.activation_function = activation_function
    self.use_batch_normalization = use_batch_normalization
    self.dropout_rate = dropout_rate
    self.data_format = data_format

  def __convolution_block(self, inputs, number_of_filters, is_training, block_name):
    concat_axis = Conv2dUtilities.channel_axis(inputs, self.data_format)
    with tf.name_scope('convolution_block_' + block_name):
      for i in range(self.number_of_convolutions_per_block):
        with tf.name_scope('convolution_' + block_name + '_' + str(i + 1)):
          layer = inputs
          if self.use_batch_normalization:
            layer = tf.layers.batch_normalization(layer, training=is_training)
          layer = self.activation_function(layer)
          layer = tf.layers.conv2d(
              inputs=layer, filters=number_of_filters, kernel_size=(3, 3), padding='same',
              data_format=self.data_format)
          if self.dropout_rate > 0.:
            layer = tf.layers.dropout(layer, rate=self.dropout_rate, training=is_training)
          inputs = tf.concat([inputs, layer], concat_axis)
    return inputs

  def __downsample(self, inputs, is_training):
    # TODO: Make the downsampling configurable (DeepBlender)
    number_of_filters = Conv2dUtilities.number_of_channels(inputs, self.data_format)
    with tf.name_scope('downsample'):
      if self.use_batch_normalization:
        inputs = tf.layers.batch_normalization(inputs, training=is_training)
      inputs = self.activation_function(inputs)
      inputs = tf.layers.conv2d(
          inputs=inputs, filters=number_of_filters, kernel_size=(1, 1), padding='same',
          data_format=self.data_format)
      if self.dropout_rate > 0.:
        inputs = tf.layers.dropout(inputs, rate=self.dropout_rate, training=is_training)
      inputs = tf.layers.max_pooling2d(
          inputs=inputs, pool_size=(2, 2), strides=(2, 2), padding='same',
          data_format=self.data_format)
    return inputs

  def __upsample(self, inputs, number_of_filters, is_training):
    with tf.name_scope('upsample'):
      inputs = tf.layers.conv2d_transpose(
          inputs=inputs, filters=number_of_filters, kernel_size=(3, 3), strides=(2, 2), padding='same',
          activation=self.activation_function, data_format=self.data_format)
      return inputs

  def predict(self, inputs, is_training):
    with tf.name_scope('Tiramisu'):
      results = []
      
      concat_axis = Conv2dUtilities.channel_axis(inputs, self.data_format)
      number_of_sampling_steps = len(self.number_of_filters_for_convolution_blocks) - 1
      downsampling_tensors = []
      
      # Preprocessing convolution
      with tf.name_scope('Preprocessing'):
        inputs = tf.layers.conv2d(
            inputs=inputs, filters=self.number_of_preprocessing_convolution_filters, kernel_size=(3, 3), padding='same',
            activation=self.activation_function, data_format=self.data_format)
      
      # Downsampling
      with tf.name_scope('downsampling'):
        for i in range(number_of_sampling_steps):
          index = i
          
          number_of_filters = self.number_of_filters_for_convolution_blocks[index]
          inputs = self.__convolution_block(inputs, number_of_filters, is_training, 'downsampling_' + str(index + 1))
          downsampling_tensors.append(inputs)
          
          inputs = self.__downsample(inputs, is_training)
      
      # Upsampling
      with tf.name_scope('upsampling'):
        for i in range(number_of_sampling_steps):
          index = number_of_sampling_steps - i
          number_of_filters = self.number_of_filters_for_convolution_blocks[index]
          inputs = self.__convolution_block(inputs, number_of_filters, is_training, 'upsampling_' + str(index + 1))
          if self.use_multiscale_output:
            results.append(inputs)
          
          inputs = self.__upsample(inputs, self.number_of_filters_for_convolution_blocks[index - 1], is_training)
          
          downsampled_tensor = downsampling_tensors[index - 1]
          inputs = tf.concat([downsampled_tensor, inputs], concat_axis)
        
        # Last convolution block
        inputs = self.__convolution_block(
            inputs, self.number_of_filters_for_convolution_blocks[0], is_training, 'upsampling_1')
        results.append(inputs)
    
    return results
