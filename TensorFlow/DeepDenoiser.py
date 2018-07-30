from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse
import os
import sys
import json
import random

import tensorflow as tf
import multiprocessing

import Utilities
from Conv2dUtilities import Conv2dUtilities
from FeatureFlags import FeatureFlags
from SourceEncoder import SourceEncoder
from KernelPrediction import KernelPrediction
from MultiScalePrediction import MultiScalePrediction

from UNet import UNet
from Tiramisu import Tiramisu

from DataAugmentation import DataAugmentation
from DataAugmentation import DataAugmentationUsage
from LossDifference import LossDifference
from LossDifference import LossDifferenceEnum
from Naming import Naming
from RenderPasses import RenderPasses
from FeatureEngineering import FeatureEngineering

parser = argparse.ArgumentParser(description='Training and inference for the DeepDenoiser.')

parser.add_argument(
    'json_filename',
    help='The json specifying all the relevant details.')

parser.add_argument(
    '--validate', action="store_true",
    help='Perform a validation step.')

parser.add_argument(
    '--threads', default=multiprocessing.cpu_count() + 1,
    help='Number of threads to use')

parser.add_argument(
    '--train_epochs', type=int, default=10000,
    help='Number of epochs to train.')

parser.add_argument(
    '--validation_interval', type=int, default=1,
    help='Number of epochs after which a validation is made.')

parser.add_argument(
    '--data_format', type=str, default=None,
    choices=['channels_first', 'channels_last'],
    help='A flag to override the data format used in the model. channels_first '
         'provides a performance boost on GPU but is not always compatible '
         'with CPU. If left unspecified, the data format will be chosen '
         'automatically based on whether TensorFlow was built for CPU or GPU.')

def global_activation_function(features, name=None):
  # HACK: Quick way to experiment with other activation function.
  return tf.nn.relu(features, name=name)
  # return tf.nn.leaky_relu(features, name=name)
  # return tf.nn.crelu(features, name=name)
  # return tf.nn.elu(features, name=name)
  # return tf.nn.selu(features, name=name)
  
  
class FeatureStandardization:

  def __init__(self, use_log1p, mean, variance, name):
    self.use_log1p = use_log1p
    self.mean = mean
    self.variance = variance
    self.name = name

  def use_mean(self):
    return self.mean != 0.
  
  def use_variance(self):
    return self.variance != 1.

  def standardize(self, feature, index):
    if self.use_log1p:
      feature = Utilities.signed_log1p(feature)
    if self.use_mean():
      feature = tf.subtract(feature, self.mean)
    if self.use_variance():
      feature = tf.divide(feature, tf.sqrt(self.variance))
    return feature
    
  def invert_standardization(self, feature):
    if self.use_variance():
      feature = tf.multiply(feature, tf.sqrt(self.variance))
    if self.use_mean():
      feature = tf.add(feature, self.mean)
    if self.use_log1p:
      feature = Utilities.signed_expm1(feature)
    return feature


class FeatureVariance:

  def __init__(self, use_variance, variance_mode, relative_variance, compute_before_standardization, compress_to_one_channel, name):
    self.use_variance = use_variance
    self.variance_mode = variance_mode
    self.relative_variance = relative_variance
    self.compute_before_standardization = compute_before_standardization
    self.compress_to_one_channel = compress_to_one_channel
    self.name = name
  
  def variance(self, inputs, epsilon=1e-4, data_format='channels_last'):
    assert self.use_variance
    result = FeatureEngineering.variance(
        inputs, variance_mode=self.variance_mode, relative_variance=self.relative_variance, compress_to_one_channel=self.compress_to_one_channel,
        epsilon=epsilon, data_format=data_format)
    return result
  

class PredictionFeature:

  def __init__(
      self, number_of_sources, is_target, feature_standardization, invert_standardization, feature_variance,
      feature_flag_names, number_of_channels, name):
    
    self.number_of_sources = number_of_sources
    self.is_target = is_target
    self.feature_standardization = feature_standardization
    self.invert_standardization = invert_standardization
    self.feature_variance = feature_variance
    self.feature_flag_names = feature_flag_names
    self.number_of_channels = number_of_channels
    self.name = name
    self.predictions = []

  def initialize_sources_from_dictionary(self, dictionary):
    self.source = []
    self.variance = []
    for index in range(self.number_of_sources):
      source_at_index = dictionary[Naming.source_feature_name(self.name, index=index)]
      self.source.append(source_at_index)

  def standardize(self):
    with tf.name_scope(Naming.tensorboard_name(self.name + ' Variance')):
      if self.feature_variance.use_variance and self.feature_variance.compute_before_standardization:
        for index in range(self.number_of_sources):
          assert len(self.variance) == index
          variance = self.feature_variance.variance(self.source[index], data_format='channels_last')
          self.variance.append(variance)
    
    with tf.name_scope(Naming.tensorboard_name('Standardize ' + self.name)):
      if self.feature_standardization != None:
        for index in range(self.number_of_sources):
          self.source[index] = self.feature_standardization.standardize(self.source[index], index)
    
    with tf.name_scope(Naming.tensorboard_name(self.name + ' Variance')):
      if self.feature_variance.use_variance and not self.feature_variance.compute_before_standardization:
        for index in range(self.number_of_sources):
          assert len(self.variance) == index
          variance = self.feature_variance.variance(self.source[index], data_format='channels_last')
          self.variance.append(variance)
  
  def prediction_invert_standardization(self):
    with tf.name_scope(Naming.tensorboard_name('Invert Standardization ' + self.name)):
      if self.feature_standardization != None:
        for index in range(len(self.predictions)):
          self.predictions[index] = self.feature_standardization.invert_standardization(self.predictions[index])
  
  def add_prediction(self, scale_index, prediction):
    if not self.is_target:
      raise Exception('Adding a prediction for a feature that is not a target is not allowed.')
    while len(self.predictions) <= scale_index:
      self.predictions.append(None)
    self.predictions[scale_index] = prediction
  
  def add_prediction_to_dictionary(self, scale_index, dictionary):
    if self.is_target:
      dictionary[Naming.prediction_feature_name(self.name)] = self.predictions[scale_index]


class BaseTrainingFeature:

  def __init__(
      self, name, loss_difference, use_multiscale_loss, use_multiscale_metrics,
      mean_weight, variation_weight, ms_ssim_weight,
      masked_mean_weight, masked_variation_weight, masked_ms_ssim_weight,
      track_mean, track_variation, track_ms_ssim,
      track_difference_histogram, track_variation_difference_histogram,
      track_masked_mean, track_masked_variation, track_masked_ms_ssim,
      track_masked_difference_histogram, track_masked_variation_difference_histogram):
      
    self.name = name
    self.loss_difference = loss_difference
    
    self.use_multiscale_loss = use_multiscale_loss
    self.use_multiscale_metrics = use_multiscale_metrics
    
    self.mean_weight = mean_weight
    self.variation_weight = variation_weight
    self.ms_ssim_weight = ms_ssim_weight
    self.masked_mean_weight = masked_mean_weight
    self.masked_variation_weight = masked_variation_weight
    self.masked_ms_ssim_weight = masked_ms_ssim_weight
    
    self.track_mean = track_mean
    self.track_variation = track_variation
    self.track_ms_ssim = track_ms_ssim
    self.track_difference_histogram = track_difference_histogram
    self.track_variation_difference_histogram = track_variation_difference_histogram
    self.track_masked_mean = track_masked_mean
    self.track_masked_variation = track_masked_variation
    self.track_masked_ms_ssim = track_masked_ms_ssim
    self.track_masked_difference_histogram = track_masked_difference_histogram
    self.track_masked_variation_difference_histogram = track_masked_variation_difference_histogram
  
  
  def difference(self, scale_index):
    with tf.name_scope(Naming.difference_name(self.name, internal=True, scale_index=scale_index)):
      result = LossDifference.difference(self.predicted[scale_index], self.target[scale_index], self.loss_difference)
    return result
  
  def masked_difference(self, scale_index):
    with tf.name_scope(Naming.difference_name(self.name, masked=True, internal=True, scale_index=scale_index)):
      result = tf.multiply(self.difference(scale_index), self.mask[scale_index])
    return result
  
  def mean(self, scale_index):
    with tf.name_scope(Naming.mean_name(self.name, internal=True, scale_index=scale_index)):
      result = tf.reduce_mean(self.difference(scale_index))
    return result
  
  def masked_mean(self, scale_index):
    with tf.name_scope(Naming.mean_name(self.name, masked=True, internal=True, scale_index=scale_index)):
      result = tf.cond(
          tf.greater(self.mask_sum[scale_index], 0.),
          lambda: tf.reduce_sum(tf.divide(self.masked_difference(scale_index), self.mask_sum[scale_index])),
          lambda: tf.constant(0.))
    return result
  
  def variation_difference(self, scale_index):
    with tf.name_scope(Naming.variation_difference_name(self.name, internal=True, scale_index=scale_index)):
      result = tf.concat(
          [tf.layers.flatten(self._horizontal_variation_difference(scale_index)),
          tf.layers.flatten(self._vertical_variation_difference(scale_index))], axis=1)
    return result
  
  def masked_variation_difference(self, scale_index):
    with tf.name_scope(Naming.variation_difference_name(self.name, masked=True, internal=True, scale_index=scale_index)):
      result = tf.multiply(self.variation_difference(scale_index), self.mask[scale_index])
    return result
    
  def variation_mean(self, scale_index):
    with tf.name_scope(Naming.variation_mean_name(self.name, internal=True, scale_index=scale_index)):
      result = tf.reduce_mean(self.variation_difference(scale_index))
    return result
    
  def masked_variation_mean(self, scale_index):
    with tf.name_scope(Naming.variation_mean_name(self.name, masked=True, internal=True, scale_index=scale_index)):
      result = tf.cond(
          tf.greater(self.mask_sum[scale_index], 0.),
          lambda: tf.reduce_sum(tf.divide(self.masked_variation_difference(scale_index), self.mask_sum[scale_index])),
          lambda: tf.constant(0.))
    return result
  
  def _horizontal_variation_difference(self, scale_index):
    predicted_horizontal_variation = BaseTrainingFeature.__horizontal_variation(self.predicted[scale_index])
    target_horizontal_variation = BaseTrainingFeature.__horizontal_variation(self.target[scale_index])
    result = LossDifference.difference(
        predicted_horizontal_variation, target_horizontal_variation, self.loss_difference)
    return result
  
  def _vertical_variation_difference(self, scale_index):
    predicted_vertical_variation = BaseTrainingFeature.__vertical_variation(self.predicted[scale_index])
    target_vertical_variation = BaseTrainingFeature.__vertical_variation(self.target[scale_index])
    result = LossDifference.difference(
        predicted_vertical_variation, target_vertical_variation, self.loss_difference)
    return result
  
  def ms_ssim(self):
    predicted = self.predicted[0]
    target = self.target[0]
    
    if len(predicted.shape) == 3:
      shape = tf.shape(predicted)
      predicted = tf.reshape(predicted, [-1, shape[0], shape[1], shape[2]])
      target = tf.reshape(target, [-1, shape[0], shape[1], shape[2]])
    
    # Move channels to last position if needed.
    if predicted.shape[3] != 3:
      predicted = Conv2dUtilities.convert_to_data_format(predicted, 'channels_first')
      target = Conv2dUtilities.convert_to_data_format(target, 'channels_first')
    
    # Our tile size is not large enough for all power factors (0.0448, 0.2856, 0.3001, 0.2363, 0.1333)
    # Starting with the second power factor, the size is scaled down by 2 after each one. The size after
    # the downscaling has to be larger than 11 which is the filter size that is used by SSIM.
    # 64 / 2 / 2 = 16 > 11
    
    # TODO: Calculate the number of factors (DeepBlender)
    # HACK: This is far away from the actual 1e10, but we are looking for better visual results. (DeepBlender)
    # maximum_value = 1e10
    maximum_value = 1.
    ms_ssim = tf.image.ssim_multiscale(predicted, target, maximum_value, power_factors=(0.0448, 0.2856, 0.3001))
    
    result = tf.subtract(1., tf.reduce_mean(ms_ssim))
    return result
  
  def masked_ms_ssim(self):
    raise Exception('Not implemented')
  
  
  def loss(self):
    result = 0.
    
    # Allow to have the loss for multiple prediction scales.
    scale_index_count = 1
    if self.use_multiscale_loss:
      scale_index_count = len(self.target)
    
    # Precalculate the common factor for the loss at the different scales.
    scale_weight_factor = 0.
    for scale_index in range(scale_index_count):
      scale_weight_factor = scale_weight_factor + (1. / (4. ** scale_index))
    scale_weight_factor = 1. / scale_weight_factor
    
    with tf.name_scope(Naming.tensorboard_name(self.name + ' Weighted Means')):
      for scale_index in range(scale_index_count):
        scale_factor = scale_weight_factor / (4. ** scale_index)
        if self.mean_weight > 0.:
          result = tf.add(result, tf.scalar_mul(self.mean_weight * scale_factor, self.mean(scale_index)))
        if self.variation_weight > 0.:
          result = tf.add(result, tf.scalar_mul(self.variation_weight * scale_factor, self.variation_mean(scale_index)))
      if self.ms_ssim_weight > 0.:
        result = tf.add(result, tf.scalar_mul(self.ms_ssim_weight, self.ms_ssim()))
    
    with tf.name_scope(Naming.tensorboard_name(self.name + ' Weighted Masked Means')):
      for scale_index in range(scale_index_count):
        scale_factor = scale_weight_factor / (4. ** scale_index)
        if self.masked_mean_weight > 0.:
          result = tf.add(result, tf.scalar_mul(self.masked_mean_weight * scale_factor, self.masked_mean(scale_index)))
        if self.masked_variation_weight > 0.:
          result = tf.add(result, tf.scalar_mul(self.masked_variation_weight * scale_factor, self.masked_variation_mean(scale_index)))
      if self.masked_ms_ssim_weight > 0.:
        result = tf.add(result, tf.scalar_mul(self.masked_ms_ssim_weight, self.masked_ms_ssim()))
    return result
    
  
  def add_tracked_summaries(self):
    scale_index_count = 1
    if self.use_multiscale_metrics:
      scale_index_count = len(self.target)
    
    for scale_index in range(scale_index_count):
      if self.track_mean:
        tf.summary.scalar(Naming.mean_name(self.name, scale_index=scale_index), self.mean(scale_index))
      if self.track_variation:
        tf.summary.scalar(Naming.variation_mean_name(self.name, scale_index=scale_index), self.variation_mean(scale_index))
    if self.track_ms_ssim:
      tf.summary.scalar(Naming.ms_ssim_name(self.name), self.ms_ssim())
    
    for scale_index in range(scale_index_count): 
      if self.track_masked_mean:
        tf.summary.scalar(Naming.mean_name(self.name, masked=True, scale_index=scale_index), self.masked_mean(scale_index))
      if self.track_masked_variation:
        tf.summary.scalar(Naming.variation_mean_name(self.name, masked=True, scale_index=scale_index), self.masked_variation_mean(scale_index))
    if self.track_masked_ms_ssim:
      tf.summary.scalar(Naming.ms_ssim_name(self.name, masked=True), self.masked_ms_ssim(scale_index))
  
  def add_tracked_histograms(self):
    scale_index_count = 1
    if self.use_multiscale_metrics:
      scale_index_count = len(self.target)
    
    for scale_index in range(scale_index_count):
      if self.track_difference_histogram:
        tf.summary.histogram(Naming.difference_name(self.name, scale_index=scale_index), self.difference(scale_index))
      if self.track_variation_difference_histogram:
        tf.summary.histogram(Naming.variation_difference_name(self.name, scale_index=scale_index), self.variation_difference(scale_index))
      
      if self.track_masked_difference_histogram:
        tf.summary.histogram(Naming.difference_name(self.name, masked=True, scale_index=scale_index), self.masked_difference(scale_index))
      if self.track_masked_variation_difference_histogram:
        tf.summary.histogram(Naming.variation_difference_name(self.name, masked=True, scale_index=scale_index), self.masked_variation_difference(scale_index))
    
  def add_tracked_metrics_to_dictionary(self, dictionary):
    scale_index_count = 1
    if self.use_multiscale_metrics:
      scale_index_count = len(self.target)
    
    for scale_index in range(scale_index_count):
      if self.track_mean:
        dictionary[Naming.mean_name(self.name, scale_index=scale_index)] = tf.metrics.mean(self.mean(scale_index))
      if self.track_variation:
        dictionary[Naming.variation_mean_name(self.name, scale_index=scale_index)] = tf.metrics.mean(self.variation_mean(scale_index))
    if self.track_ms_ssim:
      dictionary[Naming.ms_ssim_name(self.name)] = tf.metrics.mean(self.ms_ssim())
    
    for scale_index in range(scale_index_count):
      if self.track_masked_mean:
        dictionary[Naming.mean_name(self.name, masked=True, scale_index=scale_index)] = tf.metrics.mean(self.masked_mean(scale_index))
      if self.track_masked_variation:
        dictionary[Naming.variation_mean_name(self.name, masked=True, scale_index=scale_index)] = tf.metrics.mean(self.masked_variation_mean(scale_index))
    if self.track_masked_ms_ssim:
      dictionary[Naming.ms_ssim_name(self.name, masked=True)] = tf.metrics.mean(self.masked_ms_ssim())

  @staticmethod
  def __horizontal_variation(image_batch):
    # 'channels_last' or NHWC
    image_batch = tf.subtract(
        BaseTrainingFeature.__shift_left(image_batch), BaseTrainingFeature.__shift_right(image_batch))
    return image_batch
    
  def __vertical_variation(image_batch):
    # 'channels_last' or NHWC
    image_batch = tf.subtract(
        BaseTrainingFeature.__shift_up(image_batch), BaseTrainingFeature.__shift_down(image_batch))
    return image_batch
    
  @staticmethod
  def __shift_left(image_batch):
    # 'channels_last' or NHWC
    axis = 2
    width = tf.shape(image_batch)[axis]
    image_batch = tf.slice(image_batch, [0, 0, 1, 0], [-1, -1, width - 1, -1])
    return(image_batch)
  
  @staticmethod
  def __shift_right(image_batch):
    # 'channels_last' or NHWC
    axis = 2
    width = tf.shape(image_batch)[axis]
    image_batch = tf.slice(image_batch, [0, 0, 0, 0], [-1, -1, width - 1, -1]) 
    return(image_batch)
  
  @staticmethod
  def __shift_up(image_batch):
    # 'channels_last' or NHWC
    axis = 1
    height = tf.shape(image_batch)[axis]
    image_batch = tf.slice(image_batch, [0, 1, 0, 0], [-1, height - 1, -1, -1]) 
    return(image_batch)

  @staticmethod
  def __shift_down(image_batch):
    # 'channels_last' or NHWC
    axis = 1
    height = tf.shape(image_batch)[axis]
    image_batch = tf.slice(image_batch, [0, 0, 0, 0], [-1, height - 1, -1, -1]) 
    return(image_batch)


class TrainingFeature(BaseTrainingFeature):

  def __init__(
      self, name, loss_difference, use_multiscale_loss, use_multiscale_metrics,
      mean_weight, variation_weight, ms_ssim_weight,
      masked_mean_weight, masked_variation_weight, masked_ms_ssim_weight,
      track_mean, track_variation, track_ms_ssim,
      track_difference_histogram, track_variation_difference_histogram,
      track_masked_mean, track_masked_variation, track_masked_ms_ssim,
      track_masked_difference_histogram, track_masked_variation_difference_histogram):
    
    BaseTrainingFeature.__init__(
        self, name, loss_difference, use_multiscale_loss, use_multiscale_metrics,
        mean_weight, variation_weight, ms_ssim_weight,
        masked_mean_weight, masked_variation_weight, masked_ms_ssim_weight,
        track_mean, track_variation, track_ms_ssim,
        track_difference_histogram, track_variation_difference_histogram,
        track_masked_mean, track_masked_variation, track_masked_ms_ssim,
        track_masked_difference_histogram, track_masked_variation_difference_histogram)
  
  def initialize(self, source_features, predicted_features, target_features):
    self.predicted = []
    self.target = []
    self.mask = []
    self.mask_sum = []
    
    for scale_index in range(len(target_features)):
      self.predicted.append(predicted_features[scale_index][Naming.prediction_feature_name(self.name)])
      self.target.append(target_features[scale_index][Naming.target_feature_name(self.name)])
      
      corresponding_color_pass = None
      if RenderPasses.is_color_render_pass(self.name):
        corresponding_color_pass = self.name
      elif self.name == RenderPasses.ENVIRONMENT or self.name == RenderPasses.EMISSION:
        corresponding_color_pass = self.name
      elif RenderPasses.is_direct_or_indirect_render_pass(self.name):
        corresponding_color_pass = RenderPasses.direct_or_indirect_to_color_render_pass(self.name)
      
      if corresponding_color_pass != None:
        corresponding_target_feature = target_features[scale_index][Naming.target_feature_name(corresponding_color_pass)]
        self.mask.append(Conv2dUtilities.non_zero_mask(corresponding_target_feature, data_format='channels_last'))
        self.mask_sum.append(tf.reduce_sum(self.mask[scale_index]))


class CombinedTrainingFeature(BaseTrainingFeature):

  def __init__(
      self, name, loss_difference, use_multiscale_loss, use_multiscale_metrics,
      color_training_feature, direct_training_feature, indirect_training_feature,
      mean_weight, variation_weight, ms_ssim_weight,
      masked_mean_weight, masked_variation_weight, masked_ms_ssim_weight,
      track_mean, track_variation, track_ms_ssim,
      track_difference_histogram, track_variation_difference_histogram,
      track_masked_mean, track_masked_variation, track_masked_ms_ssim,
      track_masked_difference_histogram, track_masked_variation_difference_histogram):
    
    BaseTrainingFeature.__init__(
        self, name, loss_difference, use_multiscale_loss, use_multiscale_metrics,
        mean_weight, variation_weight, ms_ssim_weight,
        masked_mean_weight, masked_variation_weight, masked_ms_ssim_weight,
        track_mean, track_variation, track_ms_ssim,
        track_difference_histogram, track_variation_difference_histogram,
        track_masked_mean, track_masked_variation, track_masked_ms_ssim,
        track_masked_difference_histogram, track_masked_variation_difference_histogram)
    
    self.color_training_feature = color_training_feature
    self.direct_training_feature = direct_training_feature
    self.indirect_training_feature = indirect_training_feature
  
  def initialize(self, source_features, predicted_features, target_features):
    self.predicted = []
    self.target = []
    self.mask = []
    self.mask_sum = []
    
    for scale_index in range(len(target_features)):
      self.predicted.append(tf.multiply(
          self.color_training_feature.predicted[scale_index],
          tf.add(
              self.direct_training_feature.predicted[scale_index],
              self.indirect_training_feature.predicted[scale_index])))

      self.target.append(tf.multiply(
          self.color_training_feature.target[scale_index],
          tf.add(
              self.direct_training_feature.target[scale_index],
              self.indirect_training_feature.target[scale_index])))
      
      corresponding_color_pass = RenderPasses.combined_to_color_render_pass(self.name)
      corresponding_target_feature = target_features[scale_index][Naming.target_feature_name(corresponding_color_pass)]
      self.mask.append(Conv2dUtilities.non_zero_mask(corresponding_target_feature, data_format='channels_last'))
      self.mask_sum.append(tf.reduce_sum(self.mask[scale_index]))
  
  
class CombinedImageTrainingFeature(BaseTrainingFeature):

  def __init__(
      self, name, loss_difference, use_multiscale_loss, use_multiscale_metrics,
      diffuse_training_feature, glossy_training_feature,
      subsurface_training_feature, transmission_training_feature,
      emission_training_feature, environment_training_feature,
      mean_weight, variation_weight, ms_ssim_weight,
      masked_mean_weight, masked_variation_weight, masked_ms_ssim_weight,
      track_mean, track_variation, track_ms_ssim,
      track_difference_histogram, track_variation_difference_histogram,
      track_masked_mean, track_masked_variation, track_masked_ms_ssim,
      track_masked_difference_histogram, track_masked_variation_difference_histogram):
    
    BaseTrainingFeature.__init__(
        self, name, loss_difference, use_multiscale_loss, use_multiscale_metrics,
        mean_weight, variation_weight, ms_ssim_weight,
        masked_mean_weight, masked_variation_weight, masked_ms_ssim_weight,
        track_mean, track_variation, track_ms_ssim,
        track_difference_histogram, track_variation_difference_histogram,
        track_masked_mean, track_masked_variation, track_masked_ms_ssim,
        track_masked_difference_histogram, track_masked_variation_difference_histogram)
    
    self.diffuse_training_feature = diffuse_training_feature
    self.glossy_training_feature = glossy_training_feature
    self.subsurface_training_feature = subsurface_training_feature
    self.transmission_training_feature = transmission_training_feature
    self.emission_training_feature = emission_training_feature
    self.environment_training_feature = environment_training_feature
  
  def initialize(self, source_features, predicted_features, target_features):
    self.predicted = []
    self.target = []
    self.mask = []
    self.mask_sum = []
    
    for scale_index in range(len(target_features)):
      self.predicted.append(tf.add_n([
          self.diffuse_training_feature.predicted[scale_index],
          self.glossy_training_feature.predicted[scale_index],
          self.subsurface_training_feature.predicted[scale_index],
          self.transmission_training_feature.predicted[scale_index],
          self.emission_training_feature.predicted[scale_index],
          self.environment_training_feature.predicted[scale_index]]))

      self.target.append(tf.add_n([
          self.diffuse_training_feature.target[scale_index],
          self.glossy_training_feature.target[scale_index],
          self.subsurface_training_feature.target[scale_index],
          self.transmission_training_feature.target[scale_index],
          self.emission_training_feature.target[scale_index],
          self.environment_training_feature.target[scale_index]]))

class TrainingFeatureLoader:

  def __init__(self, is_target, number_of_channels, name):
    self.is_target = is_target
    self.number_of_channels = number_of_channels
    self.name = name
  
  def add_to_parse_dictionary(self, dictionary, required_indices):
    for index in required_indices:
      dictionary[Naming.source_feature_name(self.name, index=index)] = tf.FixedLenFeature([], tf.string)
    if self.is_target:
      dictionary[Naming.target_feature_name(self.name)] = tf.FixedLenFeature([], tf.string)
  
  def deserialize(self, parsed_features, required_indices, height, width):
    self.source = {}
    for index in required_indices:
      self.source[index] = tf.decode_raw(
          parsed_features[Naming.source_feature_name(self.name, index=index)], tf.float32)
      self.source[index] = tf.reshape(self.source[index], [height, width, self.number_of_channels])
    if self.is_target:
      self.target = tf.decode_raw(parsed_features[Naming.target_feature_name(self.name)], tf.float32)
      self.target = tf.reshape(self.target, [height, width, self.number_of_channels])
  
  def add_to_sources_dictionary(self, sources, index_tuple):
    for i in range(len(index_tuple)):
      index = index_tuple[i]
      sources[Naming.source_feature_name(self.name, index=i)] = self.source[index]
    
  def add_to_targets_dictionary(self, targets):
    if self.is_target:
      targets[Naming.target_feature_name(self.name)] = self.target


class TrainingFeatureAugmentation:

  def __init__(self, number_of_sources, is_target, number_of_channels, name):
    self.number_of_sources = number_of_sources
    self.is_target = is_target
    self.number_of_channels = number_of_channels
    self.name = name
  
  def intialize_from_dictionaries(self, sources, targets):
    self.source = {}
    for index in range(self.number_of_sources):
      self.source[index] = (sources[Naming.source_feature_name(self.name, index=index)])
    if self.is_target:
      self.target = targets[Naming.target_feature_name(self.name)]
  
  def flip_left_right(self, flip, data_format):
    if data_format != 'channels_last':
      raise Exception('Channel last is the only supported format.')
    for index in range(self.number_of_sources):
      self.source[index] = DataAugmentation.flip_left_right(self.source[index], self.name, flip)
    if self.is_target:
      self.target = DataAugmentation.flip_left_right(self.target, self.name, flip)
  
  def rotate_90(self, k, data_format):
    for index in range(self.number_of_sources):
      self.source[index] = DataAugmentation.rotate_90(self.source[index], k, self.name)
    if self.is_target:
      self.target = DataAugmentation.rotate_90(self.target, k, self.name)
  
  def permute_rgb(self, permute, data_format):
    if RenderPasses.is_rgb_color_render_pass(self.name):
      for index in range(self.number_of_sources):
        self.source[index] = DataAugmentation.permute_rgb(self.source[index], permute, self.name)
      if self.is_target:
        self.target = DataAugmentation.permute_rgb(self.target, permute, self.name)
  
  def add_to_sources_dictionary(self, sources):
    for index in range(self.number_of_sources):
      sources[Naming.source_feature_name(self.name, index=index)] = self.source[index]
    
  def add_to_targets_dictionary(self, targets):
    if self.is_target:
      targets[Naming.target_feature_name(self.name)] = self.target


class NeuralNetwork:

  def __init__(
      self, architecture='U-Net', number_of_filters_for_convolution_blocks=[128, 128, 128], number_of_convolutions_per_block=5,
      use_batch_normalization=False, dropout_rate=0., use_single_feature_prediction=False,
      feature_flags="", use_multiscale_predictions=True, invert_standardization_after_multiscale_predictions=False,
      use_kernel_predicion=True, kernel_size=5):
    self.architecture = architecture
    self.number_of_filters_for_convolution_blocks = number_of_filters_for_convolution_blocks
    self.number_of_convolutions_per_block = number_of_convolutions_per_block
    self.use_batch_normalization = use_batch_normalization
    self.dropout_rate = dropout_rate
    self.use_single_feature_prediction = use_single_feature_prediction
    self.feature_flags = feature_flags
    self.use_multiscale_predictions = use_multiscale_predictions
    self.invert_standardization_after_multiscale_predictions = invert_standardization_after_multiscale_predictions
    self.use_kernel_predicion = use_kernel_predicion
    self.kernel_size = kernel_size
  

def neural_network_model(inputs, number_of_output_channels, neural_network, is_training, data_format):

  if neural_network.architecture == 'U-Net':
    unet = UNet(
        number_of_filters_for_convolution_blocks=neural_network.number_of_filters_for_convolution_blocks,
        number_of_convolutions_per_block=neural_network.number_of_convolutions_per_block,
        use_multiscale_output=neural_network.use_multiscale_predictions,
        activation_function=global_activation_function,
        use_batch_normalization=neural_network.use_batch_normalization,
        dropout_rate=neural_network.dropout_rate,
        data_format=data_format)
    inputs = unet.unet(inputs, is_training)
  else:
    assert neural_network.architecture == 'Tiramisu'
    tiramisu = Tiramisu(
        # TODO: Make it configurable as well (DeepBlender)
        number_of_preprocessing_convolution_filters=neural_network.number_of_filters_for_convolution_blocks[0],
        
        number_of_filters_for_convolution_blocks=neural_network.number_of_filters_for_convolution_blocks,
        number_of_convolutions_per_block=neural_network.number_of_convolutions_per_block,
        use_multiscale_output=neural_network.use_multiscale_predictions,
        activation_function=global_activation_function,
        use_batch_normalization=neural_network.use_batch_normalization,
        dropout_rate=neural_network.dropout_rate,
        data_format=data_format)
    inputs = tiramisu.tiramisu(inputs, is_training)
  
  # Adjust the output to have the required number of channels.
  with tf.name_scope('Postprocess'):
    for index in range(len(inputs)):
      inputs[index] = adjust_network_output(inputs[index], number_of_output_channels, data_format)
  
  return inputs

def adjust_network_output(inputs, number_of_output_channels, data_format):
  result = tf.layers.conv2d(
      inputs=inputs, filters=number_of_output_channels, kernel_size=(1, 1), padding='same',
      activation=global_activation_function, data_format=data_format)
  result = tf.layers.conv2d(
      inputs=result, filters=number_of_output_channels, kernel_size=(1, 1), padding='same',
      activation=None, data_format=data_format)
  return result

def combined_features_model(prediction_features, output_prediction_features, is_training, neural_network, use_CPU_only, data_format):
  
  # TODO: Add SourceEncoder (DeepBlender)
  
  raise Exception('Do not use this at the moment!')
  
  source_data_format = 'channels_last'
  source_concat_axis = Conv2dUtilities.channel_axis(prediction_features[0].source[0], data_format)
  
  with tf.name_scope('bundle_features'):
    prediction_inputs = []
    auxiliary_prediction_inputs = []
    auxiliary_inputs = []
    for prediction_feature in prediction_features:
      if prediction_feature.is_target:
        for index in range(prediction_feature.number_of_sources):
          source = prediction_feature.source[index]
          if index == 0:
            prediction_inputs.append(source)
            if prediction_feature.feature_variance.use_variance:
              source_variance = prediction_feature.variance[index]
              prediction_inputs.append(source_variance)
          else:
            auxiliary_prediction_inputs.append(source)
            if prediction_feature.feature_variance.use_variance:
              source_variance = prediction_feature.variance[index]
              auxiliary_prediction_inputs.append(source_variance)
      else:
        for index in range(prediction_feature.number_of_sources):
          source = prediction_feature.source[index]
          auxiliary_inputs.append(source)
          if prediction_feature.feature_variance.use_variance:
            source_variance = prediction_feature.variance[index]
            auxiliary_inputs.append(source_variance)
          
    prediction_inputs = tf.concat(prediction_inputs, source_concat_axis)
    
    if len(auxiliary_prediction_inputs) > 0:
      auxiliary_prediction_inputs = tf.concat(auxiliary_prediction_inputs, source_concat_axis)
    else:
      auxiliary_prediction_inputs = None
    if len(auxiliary_inputs) > 0:
      auxiliary_inputs = tf.concat(auxiliary_inputs, source_concat_axis)
    else:
      auxiliary_inputs = None


  with tf.name_scope('data_format_conversion'):    
    if data_format is None:
      # When running on GPU, transpose the data from channels_last (NHWC) to
      # channels_first (NCHW) to improve performance.
      # See https://www.tensorflow.org/performance/performance_guide#data_formats
      data_format = (
        'channels_first' if tf.test.is_built_with_cuda() else
          'channels_last')
      if use_CPU_only:
        data_format = 'channels_last'
    
    if data_format != source_data_format:
      prediction_inputs = Conv2dUtilities.convert_to_data_format(prediction_inputs, data_format)
      if auxiliary_prediction_inputs != None:
        auxiliary_prediction_inputs = Conv2dUtilities.convert_to_data_format(auxiliary_prediction_inputs, data_format)
      if auxiliary_inputs != None:
        auxiliary_inputs = Conv2dUtilities.convert_to_data_format(auxiliary_inputs, data_format)
  
  
  with tf.name_scope('feature_concatenation'):
    number_of_output_channels = 0
    for prediction_feature in output_prediction_features:
      if neural_network.use_kernel_predicion:
        number_of_output_channels = number_of_output_channels + (neural_network.kernel_size ** 2)
      else:
        number_of_output_channels = number_of_output_channels + prediction_feature.number_of_channels
  
    concat_axis = Conv2dUtilities.channel_axis(prediction_inputs, data_format)
    
    if auxiliary_prediction_inputs == None and auxiliary_inputs == None:
      outputs = prediction_inputs
    elif auxiliary_prediction_inputs == None:
      outputs = tf.concat([prediction_inputs, auxiliary_inputs], concat_axis)
    elif auxiliary_inputs == None:
      outputs = tf.concat([prediction_inputs, auxiliary_prediction_inputs], concat_axis)
    else:
      outputs = tf.concat([prediction_inputs, auxiliary_prediction_inputs, auxiliary_inputs], concat_axis)
  
  
  # HACK: Implementation trick to keep tensorboard cleaner.
  # with tf.name_scope('model'):
  with tf.variable_scope('model'):
    outputs = neural_network_model(outputs, number_of_output_channels, neural_network, is_training, data_format)
    if neural_network.use_multiscale_predictions:
      # Reverse the outputs, such that it is sorted from largest to smallest.
      outputs = list(reversed(outputs))

  
  with tf.name_scope('split'):
    size_splits = []
    for prediction_feature in output_prediction_features:
      if neural_network.use_kernel_predicion:
        size_splits.append(neural_network.kernel_size ** 2)
      else:
        size_splits.append(prediction_feature.number_of_channels)
  
    predictions_tuple = []
    for output in outputs:
      prediction_tuple = tf.split(output, size_splits, concat_axis)
      predictions_tuple.append(prediction_tuple)
  
  for scale_index in range(len(predictions_tuple)):
    prediction_tuple = predictions_tuple[scale_index]
    for index, prediction in enumerate(prediction_tuple):
      output_prediction_features[index].add_prediction(scale_index, prediction)

  
  with tf.name_scope('kernel_predicions'):
    if neural_network.use_kernel_predicion:
      for prediction_feature in output_prediction_features:
        source = prediction_feature.source[0]
        
        if data_format != source_data_format:
          source = Conv2dUtilities.convert_to_data_format(source, data_format)
        
        for scale_index in range(len(prediction_feature.predictions)):
          scaled_source = source
          
          if scale_index > 0:
            size = 2 ** scale_index
            scaled_source = MultiScalePrediction.scale_down(scaled_source, heigh_width_scale_factor=size, data_format=data_format)
          with tf.name_scope(Naming.tensorboard_name(prediction_feature.name + ' Kernel Prediction')):
            prediction = KernelPrediction.kernel_prediction(
                scaled_source, prediction_feature.predictions[scale_index],
                neural_network.kernel_size, data_format=data_format)
          prediction_feature.add_prediction(scale_index, prediction)
  
  
  if not neural_network.invert_standardization_after_multiscale_predictions:
    with tf.name_scope('invert_standardization'):
      for prediction_feature in output_prediction_features:
        if prediction_feature.invert_standardization:
          prediction_feature.prediction_invert_standardization()
  
  
  with tf.name_scope('combine_multiscales'):
    if neural_network.use_multiscale_predictions:
      reuse = False
      for prediction_feature in output_prediction_features:
        for scale_index in range(len(prediction_feature.predictions) - 1, 0, -1):
          larger_scale_index = scale_index - 1
          
          small_prediction = prediction_feature.predictions[scale_index]
          prediction = prediction_feature.predictions[larger_scale_index]
          
          with tf.variable_scope('reused_compose_scales', reuse=reuse):
            prediction = MultiScalePrediction.compose_scales(small_prediction, prediction, data_format=data_format)
          reuse = True
          
          prediction_feature.add_prediction(larger_scale_index, prediction)
  
  if neural_network.invert_standardization_after_multiscale_predictions:
    with tf.name_scope('invert_standardization'):
      for prediction_feature in output_prediction_features:
        if prediction_feature.invert_standardization:
          prediction_feature.prediction_invert_standardization()

  
  # Convert back to the source data format if needed.
  with tf.name_scope('revert_data_format_conversion'):
    if data_format != source_data_format:
      for prediction_feature in output_prediction_features:
        for scale_index in range(len(prediction_feature.predictions)):
          prediction_feature.predictions[scale_index] = Conv2dUtilities.convert_to_data_format(prediction_feature.predictions[scale_index], source_data_format)


def single_feature_model(prediction_features, output_prediction_features, is_training, neural_network, use_CPU_only, data_format):
  
  source_data_format = 'channels_last'
  source_concat_axis = Conv2dUtilities.channel_axis(prediction_features[0].source[0], source_data_format)
  
  if data_format is None:
    # When running on GPU, transpose the data from channels_last (NHWC) to
    # channels_first (NCHW) to improve performance.
    # See https://www.tensorflow.org/performance/performance_guide#data_formats
    data_format = (
      'channels_first' if tf.test.is_built_with_cuda() else
        'channels_last')
    if use_CPU_only:
      data_format = 'channels_last'
  
  number_of_main_network_input_channels = neural_network.number_of_filters_for_convolution_blocks[0]
  
  with tf.name_scope('single_feature_prediction'):
    reuse_encoded_source = False
    reuse = False
    multiscale_combine_reuse = False
    
    source_features = []
    for source_feature in prediction_features:
      if not source_feature.is_target:
        source_features.append(source_feature)
    
    encoded_source = None
    for prediction_feature in prediction_features:
      if prediction_feature.is_target:
        
        if neural_network.use_kernel_predicion:
          number_of_output_channels = neural_network.kernel_size ** 2
        else:
          number_of_output_channels = prediction_feature.number_of_channels
        
        with tf.name_scope('prepare_network_input'):
          for index in range(prediction_feature.number_of_sources):
            prediction_inputs = []
            source = prediction_feature.source[index]
            prediction_inputs.append(source)
            if prediction_feature.feature_variance.use_variance:
              source_variance = prediction_feature.variance[index]
              prediction_inputs.append(source_variance)
            for source_feature in source_features:
              source = source_feature.source[index]
              prediction_inputs.append(source)
              if source_feature.feature_variance.use_variance:
                source_variance = source_feature.variance[index]
                prediction_inputs.append(source_variance)
        
            outputs = tf.concat(prediction_inputs, source_concat_axis)
          
            # Adding the prediction feature flags does only work when the tensor is unbatched. This can be achieved with 'map_fn'.
            def add_prediction_feature_flags(inputs):
              local_concat_axis = Conv2dUtilities.channel_axis(inputs, source_data_format)
              inputs = tf.concat([prediction_feature_flags, inputs], local_concat_axis)
              return inputs
            
            height, width = Conv2dUtilities.height_width(outputs, source_data_format)
            prediction_feature_flags = neural_network.feature_flags.feature_flags(prediction_feature.name, height, width, source_data_format)
            outputs = tf.map_fn(add_prediction_feature_flags, outputs)
        
            with tf.name_scope('data_format_conversion'):
              if data_format != source_data_format:
                outputs = Conv2dUtilities.convert_to_data_format(outputs, data_format)
                concat_axis = Conv2dUtilities.channel_axis(outputs, data_format)
        
            # TODO: No source encoding for now. More details in SourceEncoder (DeepBlender)
            use_source_encoder = False
            if use_source_encoder:
              with tf.variable_scope('reused_source_encoder', reuse=reuse_encoded_source):
                encoded_source = SourceEncoder.source_encoding(
                    outputs, index, number_of_main_network_input_channels, encoded_source=encoded_source,
                    activation_function=global_activation_function, data_format=data_format)
                reuse_encoded_source = True
            else:
              encoded_source = outputs
        
        with tf.name_scope('model'):
          with tf.variable_scope('reused_model', reuse=reuse):
            outputs = neural_network_model(encoded_source, number_of_output_channels, neural_network, is_training, data_format)
            if neural_network.use_multiscale_predictions:
              # Reverse the outputs, such that it is sorted from largest to smallest.
              outputs = list(reversed(outputs))
        
        # Reuse the variables after the first pass.
        reuse = True
        
        for scale_index in range(len(outputs)):
          prediction_feature.add_prediction(scale_index, outputs[scale_index])
        
        with tf.name_scope(Naming.tensorboard_name('kernel_prediction_' + prediction_feature.name)):
          if neural_network.use_kernel_predicion:
            source = prediction_feature.source[0]
            
            if data_format != source_data_format:
              source = Conv2dUtilities.convert_to_data_format(source, data_format)

            for scale_index in range(len(prediction_feature.predictions)):
              scaled_source = source
              if scale_index > 0:
                size = 2 ** scale_index
                scaled_source = MultiScalePrediction.scale_down(scaled_source, heigh_width_scale_factor=size, data_format=data_format)
              with tf.name_scope(Naming.tensorboard_name('kernel_prediction_' + prediction_feature.name)):
                prediction = KernelPrediction.kernel_prediction(
                    scaled_source, prediction_feature.predictions[scale_index],
                    neural_network.kernel_size, data_format=data_format)
              prediction_feature.add_prediction(scale_index, prediction)
        
        
        if not neural_network.invert_standardization_after_multiscale_predictions:
          with tf.name_scope('invert_standardization'):
            if prediction_feature.invert_standardization:
              prediction_feature.prediction_invert_standardization()
        
        with tf.name_scope('combine_multiscales'):
          if neural_network.use_multiscale_predictions:
            for scale_index in range(len(prediction_feature.predictions) - 1, 0, -1):
              larger_scale_index = scale_index - 1
              
              small_prediction = prediction_feature.predictions[scale_index]
              prediction = prediction_feature.predictions[larger_scale_index]
              
              with tf.variable_scope('reused_compose_scales', reuse=multiscale_combine_reuse):
                prediction = MultiScalePrediction.compose_scales(small_prediction, prediction, data_format=data_format)
              multiscale_combine_reuse = True
              
              prediction_feature.add_prediction(larger_scale_index, prediction)
        
        if neural_network.invert_standardization_after_multiscale_predictions:
          with tf.name_scope('invert_standardization'):
            if prediction_feature.invert_standardization:
              prediction_feature.prediction_invert_standardization()
        
        
        # Convert back to the source data format if needed.
        with tf.name_scope('revert_data_format_conversion'):
          if data_format != source_data_format:
            for scale_index in range(len(prediction_feature.predictions)):
              prediction_feature.predictions[scale_index] = Conv2dUtilities.convert_to_data_format(prediction_feature.predictions[scale_index], source_data_format)


def model(prediction_features, mode, neural_network, use_CPU_only, data_format):
  
  is_training = False
  if mode == tf.estimator.ModeKeys.TRAIN:
    is_training = True
  
  output_prediction_features = []
  for prediction_feature in prediction_features:
    if prediction_feature.is_target:
      output_prediction_features.append(prediction_feature)
        
  # Standardization of the data
  with tf.name_scope('standardize'):
    for prediction_feature in prediction_features:
      prediction_feature.standardize()

  if neural_network.use_single_feature_prediction:
    single_feature_model(
        prediction_features, output_prediction_features, is_training, neural_network, use_CPU_only, data_format)
  else:
    combined_features_model(
        prediction_features, output_prediction_features, is_training, neural_network, use_CPU_only, data_format)
  
  prediction_dictionaries = []
  for scale_index in range(len(output_prediction_features[0].predictions)):
    prediction_dictionary = {}
    for prediction_feature in output_prediction_features:
      prediction_feature.add_prediction_to_dictionary(scale_index, prediction_dictionary)
    prediction_dictionaries.append(prediction_dictionary)
      
  return prediction_dictionaries


def model_fn(features, labels, mode, params):
  prediction_features = params['prediction_features']
  
  for prediction_feature in prediction_features:
    prediction_feature.initialize_sources_from_dictionary(features)
  
  neural_network = params['neural_network']
  
  data_format = params['data_format']
  predictions = model(
      prediction_features, mode, neural_network,
      params['use_CPU_only'], data_format)

  if mode == tf.estimator.ModeKeys.PREDICT:
    predictions = predictions[0]
    return tf.estimator.EstimatorSpec(mode=mode, predictions=predictions)
  
  
  # Produce scaled targets if needed for the loss and metrics.
  prepare_multiscale_targets = params['use_multiscale_loss'] or params['use_multiscale_metrics']
  targets = []
  targets.append(labels)
  if prepare_multiscale_targets:
    for scale_index in range(len(predictions)):
      if scale_index > 0:
        size = 2 ** scale_index
        scaled_targets = {}
        for key in labels:
          scaled_target = MultiScalePrediction.scale_down(labels[key], heigh_width_scale_factor=size, data_format=data_format)
          scaled_targets[key] = scaled_target
        targets.append(scaled_targets)
  
  with tf.name_scope('loss_function'):
    with tf.name_scope('feature_loss'):
      training_features = params['training_features']
      for training_feature in training_features:
        training_feature.initialize(features, predictions, targets)
      feature_losses = []
      for training_feature in training_features:
        feature_losses.append(training_feature.loss())
      if len(feature_losses) > 0:
        feature_loss = tf.add_n(feature_losses)
      else:
        feature_loss = 0.
    
    with tf.name_scope('combined_feature_loss'):
      combined_training_features = params['combined_training_features']
      if combined_training_features != None:
        for combined_training_feature in combined_training_features:
          combined_training_feature.initialize(features, predictions, targets)
        combined_feature_losses = []
        for combined_training_feature in combined_training_features:
          combined_feature_losses.append(combined_training_feature.loss())
        if len(combined_feature_losses) > 0:
          combined_feature_loss = tf.add_n(combined_feature_losses)
      else:
        combined_feature_loss = 0.
    
    with tf.name_scope('combined_image_loss'):
      combined_image_training_feature = params['combined_image_training_feature']
      if combined_image_training_feature != None:
        combined_image_training_feature.initialize(features, predictions, targets)
        combined_image_feature_loss = combined_image_training_feature.loss()
      else:
        combined_image_feature_loss = 0.
    
    # All losses combined
    loss = tf.add_n([feature_loss, combined_feature_loss, combined_image_feature_loss])

  
  # Configure the training op
  if mode == tf.estimator.ModeKeys.TRAIN:
    learning_rate = params['learning_rate']
    global_step = tf.train.get_or_create_global_step()
    first_decay_steps = 1000
    t_mul = 1.3 # Use t_mul more steps after each restart.
    m_mul = 0.8 # Multiply the learning rate after each restart with this number.
    alpha = 1 / 100. # Learning rate decays from 1 * learning_rate to alpha * learning_rate.
    learning_rate_decayed = tf.train.cosine_decay_restarts(
        learning_rate, global_step, first_decay_steps,
        t_mul=t_mul, m_mul=m_mul, alpha=alpha)
  
    tf.summary.scalar('learning_rate', learning_rate_decayed)
    tf.summary.scalar('batch_size', params['batch_size'])
    
    # Histograms
    for training_feature in training_features:
      training_feature.add_tracked_histograms()
    if combined_training_features != None:
      for combined_training_feature in combined_training_features:
        combined_training_feature.add_tracked_histograms()
    if combined_image_training_feature != None:
      combined_image_training_feature.add_tracked_histograms()
    
    # Summaries
    #with tf.name_scope('feature_summaries'):
    for training_feature in training_features:
      training_feature.add_tracked_summaries()
    #with tf.name_scope('combined_feature_summaries'):
    if combined_training_features != None:
      for combined_training_feature in combined_training_features:
        combined_training_feature.add_tracked_summaries()
    #with tf.name_scope('combined_summaries'):
    if combined_image_training_feature != None:
      combined_image_training_feature.add_tracked_summaries()
    
    with tf.name_scope('optimizer'):
      optimizer = tf.train.AdamOptimizer(learning_rate_decayed)
      train_op = optimizer.minimize(loss, global_step)
      eval_metric_ops = None
  else:
    train_op = None
    eval_metric_ops = {}

    #with tf.name_scope('features'):
    for training_feature in training_features:
      training_feature.add_tracked_metrics_to_dictionary(eval_metric_ops)
    
    #with tf.name_scope('combined_features'):
    if combined_training_features != None:
      for combined_training_feature in combined_training_features:
        combined_training_feature.add_tracked_metrics_to_dictionary(eval_metric_ops)
    
    #with tf.name_scope('combined'):
    if combined_image_training_feature != None:
      combined_image_training_feature.add_tracked_metrics_to_dictionary(eval_metric_ops)
    
  return tf.estimator.EstimatorSpec(
      mode=mode,
      loss=loss,
      train_op=train_op,
      eval_metric_ops=eval_metric_ops)


def input_fn_tfrecords(
    files, training_features_loader, training_features_augmentation, number_of_epochs, index_tuples, required_indices, data_augmentation_usage,
    tiles_height_width, batch_size, threads, data_format='channels_last'):
  
  def fast_feature_parser(serialized_example):
    assert len(index_tuples) == 1
    
    # Load all the required indices.
    features = {}
    for training_feature_loader in training_features_loader:
      training_feature_loader.add_to_parse_dictionary(features, required_indices)
    
    parsed_features = tf.parse_single_example(serialized_example, features)
    
    for training_feature_loader in training_features_loader:
      training_feature_loader.deserialize(parsed_features, required_indices, tiles_height_width, tiles_height_width)
    
    # Prepare the examples.
    index_tuple = index_tuples[0]
    
    sources = {}
    targets = {}
    for training_feature_loader in training_features_loader:
      training_feature_loader.add_to_sources_dictionary(sources, index_tuple)
      training_feature_loader.add_to_targets_dictionary(targets)
    
    return sources, targets
  
  def feature_parser(serialized_example):
    dataset = None
    
    # Load all the required indices.
    features = {}
    for training_feature_loader in training_features_loader:
      training_feature_loader.add_to_parse_dictionary(features, required_indices)
    
    parsed_features = tf.parse_single_example(serialized_example, features)
    
    for training_feature_loader in training_features_loader:
      training_feature_loader.deserialize(parsed_features, required_indices, tiles_height_width, tiles_height_width)
    
    # Prepare the examples.
    for index_tuple in index_tuples:
      sources = {}
      targets = {}
      for training_feature_loader in training_features_loader:
        training_feature_loader.add_to_sources_dictionary(sources, index_tuple)
        training_feature_loader.add_to_targets_dictionary(targets)
      
      if dataset == None:
        dataset = tf.data.Dataset.from_tensors((sources, targets))
      else:
        dataset = dataset.concatenate(tf.data.Dataset.from_tensors((sources, targets)))
    
    return dataset
  
  def data_augmentation(sources, targets):
    with tf.name_scope('data_augmentation'):
      flip = tf.random_uniform([1], minval=0, maxval=2, dtype=tf.int32)[0]
      rotate = tf.random_uniform([1], minval=0, maxval=4, dtype=tf.int32)[0]
      permute = tf.random_uniform([1], minval=0, maxval=6, dtype=tf.int32)[0]
      
      for training_feature_augmentation in training_features_augmentation:
        training_feature_augmentation.intialize_from_dictionaries(sources, targets)
        
        if data_augmentation_usage.use_flip_left_right:
          training_feature_augmentation.flip_left_right(flip, data_format)
        
        if data_augmentation_usage.use_rotate_90:
          training_feature_augmentation.rotate_90(rotate, data_format)

        if data_augmentation_usage.use_rgb_permutation:
          training_feature_augmentation.permute_rgb(permute, data_format)
    
        training_feature_augmentation.add_to_sources_dictionary(sources)
        training_feature_augmentation.add_to_targets_dictionary(targets)
    
    return sources, targets
  
  
  # REMARK: Due to stability issues, it was not possible to follow all the suggestions from the documentation like using the fused versions.
  
  shuffle_buffer_size = 10000
  files = files.repeat(number_of_epochs)
  files = files.shuffle(buffer_size=shuffle_buffer_size)
  
  dataset = tf.data.TFRecordDataset(files, compression_type='GZIP', buffer_size=None, num_parallel_reads=threads)
  if len(index_tuples) == 1:
    dataset = dataset.map(map_func=fast_feature_parser, num_parallel_calls=threads)
  else:
    dataset = dataset.flat_map(map_func=feature_parser)
  dataset = dataset.map(map_func=data_augmentation, num_parallel_calls=threads)
  
  shuffle_buffer_size = 20 * batch_size
  dataset = dataset.shuffle(buffer_size=shuffle_buffer_size)
  
  dataset = dataset.batch(batch_size)
  
  prefetch_buffer_size = 1
  dataset = dataset.prefetch(buffer_size=prefetch_buffer_size)
  
  iterator = dataset.make_one_shot_iterator()
  
  # `features` is a dictionary in which each value is a batch of values for
  # that feature; `target` is a batch of targets.
  features, targets = iterator.get_next()
  return features, targets


def train(
    tfrecords_directory, estimator, training_features_loader, training_features_augmentation,
    number_of_epochs, index_tuples, required_indices, data_augmentation_usage, tiles_height_width, batch_size, threads):
  
  files = tf.data.Dataset.list_files(tfrecords_directory + '/*')

  # Train the model
  estimator.train(input_fn=lambda: input_fn_tfrecords(
      files, training_features_loader, training_features_augmentation, number_of_epochs, index_tuples, required_indices, data_augmentation_usage,
      tiles_height_width, batch_size, threads))

def evaluate(
    tfrecords_directory, estimator, training_features_loader, training_features_augmentation,
    index_tuples, required_indices, data_augmentation_usage, tiles_height_width, batch_size, threads):
  
  files = tf.data.Dataset.list_files(tfrecords_directory + '/*')

  # Evaluate the model
  estimator.evaluate(input_fn=lambda: input_fn_tfrecords(
      files, training_features_loader, training_features_augmentation, 1, index_tuples, required_indices, data_augmentation_usage,
      tiles_height_width, batch_size, threads))

def source_index_tuples(number_of_sources_per_example, number_of_source_index_tuples, number_of_sources_per_target):
  if number_of_sources_per_example < number_of_sources_per_target:
    raise Exception('The source index tuples contain unique indices. That is not possible if there are fewer source examples than indices per tuple.')
  
  index_tuples = []
  if number_of_sources_per_target == 1:
    number_of_complete_tuple_sets = number_of_source_index_tuples // number_of_sources_per_example
    number_of_remaining_tuples = number_of_source_index_tuples % number_of_sources_per_example
    for _ in range(number_of_complete_tuple_sets):
      for index in range(number_of_sources_per_example):
        index_tuples.append([index])
    for _ in range(number_of_remaining_tuples):
      index = random.randint(0, number_of_sources_per_example - 1)
      index_tuples.append([index])
  else:
  
    raise Exception('Multiple source inputs are currently not supported!')
  
    for _ in range(number_of_source_index_tuples):
      tuple = []
      while len(tuple) < number_of_sources_per_target:
        index = random.randint(0, number_of_sources_per_example - 1)
        if not index in tuple:
          tuple.append(index)
      index_tuples.append(tuple)
  
  required_indices = []
  for tuple in index_tuples:
    for index in tuple:
      if not index in required_indices:
        required_indices.append(index)
  required_indices.sort()
  
  return index_tuples, required_indices
  

def main(parsed_arguments):
  if not isinstance(parsed_arguments.threads, int):
    parsed_arguments.threads = int(parsed_arguments.threads)

  try:
    json_filename = parsed_arguments.json_filename
    json_content = open(json_filename, 'r').read()
    parsed_json = json.loads(json_content)
  except:
    print('Expected a valid json file as argument.')
  
  
  model_directory = parsed_json['model_directory']
  base_tfrecords_directory = parsed_json['base_tfrecords_directory']
  modes = parsed_json['modes']
  
  
  neural_network = parsed_json['neural_network']
  architecture = neural_network['architecture']
  number_of_filters_for_convolution_blocks = neural_network['number_of_filters_for_convolution_blocks']
  number_of_convolutions_per_block = neural_network['number_of_convolutions_per_block']
  use_batch_normalization = neural_network['use_batch_normalization']
  dropout_rate = neural_network['dropout_rate']
  use_single_feature_prediction = neural_network['use_single_feature_prediction']
  feature_flags = FeatureFlags(neural_network['feature_flags'])
  use_multiscale_predictions = neural_network['use_multiscale_predictions']
  invert_standardization_after_multiscale_predictions = neural_network['invert_standardization_after_multiscale_predictions']
  use_multiscale_loss = neural_network['use_multiscale_loss']
  use_multiscale_metrics = neural_network['use_multiscale_metrics']
  use_kernel_predicion = neural_network['use_kernel_predicion']
  kernel_size = neural_network['kernel_size']
  
  number_of_sources_per_target = parsed_json['number_of_sources_per_target']
  number_of_source_index_tuples = parsed_json['number_of_source_index_tuples']
  
  data_augmentation = parsed_json['data_augmentation']
  data_augmentation_usage = DataAugmentationUsage(
      data_augmentation['use_rotate_90'], data_augmentation['use_flip_left_right'], data_augmentation['use_rgb_permutation'])
  
  loss_difference = parsed_json['loss_difference']
  loss_difference = LossDifferenceEnum[loss_difference]
  
  learning_rate = parsed_json['learning_rate']
  batch_size = parsed_json['batch_size']
  
  features = parsed_json['features']
  combined_features = parsed_json['combined_features']
  combined_image = parsed_json['combined_image']
  
  # The names have to be sorted, otherwise the channels would be randomly mixed.
  feature_names = sorted(list(features.keys()))
  
  
  training_tfrecords_directory = os.path.join(base_tfrecords_directory, 'training')
  validation_tfrecords_directory = os.path.join(base_tfrecords_directory, 'validation')
  
  if not 'training' in modes:
    raise Exception('No training mode found.')
  if not 'validation' in modes:
    raise Exception('No validation mode found.')
  training_statistics_filename = os.path.join(base_tfrecords_directory, 'training.json')
  validation_statistics_filename = os.path.join(base_tfrecords_directory, 'validation.json')
  
  training_statistics_content = open(training_statistics_filename, 'r').read()
  training_statistics = json.loads(training_statistics_content)
  validation_statistics_content = open(validation_statistics_filename, 'r').read()
  validation_statistics = json.loads(validation_statistics_content)
  
  training_tiles_height_width = training_statistics['tiles_height_width']
  training_number_of_sources_per_example = training_statistics['number_of_sources_per_example']
  validation_tiles_height_width = validation_statistics['tiles_height_width']
  validation_number_of_sources_per_example = validation_statistics['number_of_sources_per_example']
  
  
  prediction_features = []
  for feature_name in feature_names:
    feature = features[feature_name]
    
    # REMARK: It is assumed that there are no features which are only a target, without also being a source.
    if feature['is_source']:
      feature_variance = feature['feature_variance']
      feature_variance = FeatureVariance(
          feature_variance['use_variance'], feature_variance['variance_mode'], feature_variance['relative_variance'],
          feature_variance['compute_before_standardization'], feature_variance['compress_to_one_channel'],
          feature_name)
      feature_standardization = feature['standardization']
      feature_standardization = FeatureStandardization(
          feature_standardization['use_log1p'], feature_standardization['mean'], feature_standardization['variance'],
          feature_name)
      invert_standardization = feature['invert_standardization']
      prediction_feature = PredictionFeature(
          number_of_sources_per_target, feature['is_target'], feature_standardization, invert_standardization, feature_variance,
          feature['feature_flags'], feature['number_of_channels'], feature_name)
      prediction_features.append(prediction_feature)
  
  if use_single_feature_prediction:
    for prediction_feature in prediction_features:
      if prediction_feature.is_target:
        feature_flags.add_render_pass_name_to_feature_flag_names(prediction_feature.name, prediction_feature.feature_flag_names)
    feature_flags.freeze()
  
  # Training features.

  training_features = []
  feature_name_to_training_feature = {}
  for feature_name in feature_names:
    feature = features[feature_name]
    if feature['is_source'] and feature['is_target']:
      
      # Training loss
      loss_weights = feature['loss_weights']
      loss_weights_masked = feature['loss_weights_masked']
      
      # Training metrics
      statistics = feature['statistics']
      statistics_masked = feature['statistics_masked']
        
      training_feature = TrainingFeature(
          feature_name, loss_difference, use_multiscale_loss, use_multiscale_metrics,
          loss_weights['mean'], loss_weights['variation'], loss_weights['ms_ssim'],
          loss_weights_masked['mean'], loss_weights_masked['variation'], loss_weights_masked['ms_ssim'],
          statistics['track_mean'], statistics['track_variation'], statistics['track_ms_ssim'],
          statistics['track_difference_histogram'], statistics['track_variation_difference_histogram'],
          statistics_masked['track_mean'], statistics_masked['track_variation'], statistics_masked['track_ms_ssim'],
          statistics_masked['track_difference_histogram'], statistics_masked['track_variation_difference_histogram'])
      training_features.append(training_feature)
      feature_name_to_training_feature[feature_name] = training_feature

  training_features_loader = []
  training_features_augmentation = []
  for prediction_feature in prediction_features:
    training_features_loader.append(TrainingFeatureLoader(
        prediction_feature.is_target, prediction_feature.number_of_channels, prediction_feature.name))
    training_features_augmentation.append(TrainingFeatureAugmentation(
        number_of_sources_per_target, prediction_feature.is_target,
        prediction_feature.number_of_channels, prediction_feature.name))

  
  # Combined training features.
  
  combined_training_features = []
  combined_feature_name_to_combined_training_feature = {}
  combined_feature_names = list(combined_features.keys())
  for combined_feature_name in combined_feature_names:
    combined_feature = combined_features[combined_feature_name]
    
    # Training loss
    loss_weights = combined_feature['loss_weights']
    loss_weights_masked = combined_feature['loss_weights_masked']
    
    # Training metrics
    statistics = combined_feature['statistics']
    statistics_masked = combined_feature['statistics_masked']
    
    if loss_weights['mean'] > 0. or loss_weights['variation'] > 0.:
      color_feature_name = RenderPasses.combined_to_color_render_pass(combined_feature_name)
      direct_feature_name = RenderPasses.combined_to_direct_render_pass(combined_feature_name)
      indirect_feature_name = RenderPasses.combined_to_indirect_render_pass(combined_feature_name)
      combined_training_feature = CombinedTrainingFeature(
          combined_feature_name, loss_difference, use_multiscale_loss, use_multiscale_metrics,
          feature_name_to_training_feature[color_feature_name],
          feature_name_to_training_feature[direct_feature_name],
          feature_name_to_training_feature[indirect_feature_name],
          loss_weights['mean'], loss_weights['variation'], loss_weights['ms_ssim'],
          loss_weights_masked['mean'], loss_weights_masked['variation'], loss_weights_masked['ms_ssim'],
          statistics['track_mean'], statistics['track_variation'], statistics['track_ms_ssim'],
          statistics['track_difference_histogram'], statistics['track_variation_difference_histogram'],
          statistics_masked['track_mean'], statistics_masked['track_variation'], statistics_masked['track_ms_ssim'],
          statistics_masked['track_difference_histogram'], statistics_masked['track_variation_difference_histogram'])
      combined_training_features.append(combined_training_feature)
      combined_feature_name_to_combined_training_feature[combined_feature_name] = combined_training_feature
        
  if len(combined_training_features) == 0:
    combined_training_features = None
  
  
  # Combined image training feature.
  
  combined_image_training_feature = None
  statistics = combined_image['statistics']
  loss_weights = combined_image['loss_weights']
  if loss_weights['mean'] > 0. or loss_weights['variation'] > 0:
    combined_image_training_feature = CombinedImageTrainingFeature(
        RenderPasses.COMBINED, loss_difference, use_multiscale_loss, use_multiscale_metrics,
        combined_feature_name_to_combined_training_feature[RenderPasses.COMBINED_DIFFUSE],
        combined_feature_name_to_combined_training_feature[RenderPasses.COMBINED_GLOSSY],
        combined_feature_name_to_combined_training_feature[RenderPasses.COMBINED_SUBSURFACE],
        combined_feature_name_to_combined_training_feature[RenderPasses.COMBINED_TRANSMISSION],
        feature_name_to_training_feature[RenderPasses.EMISSION],
        feature_name_to_training_feature[RenderPasses.ENVIRONMENT],
        loss_weights['mean'], loss_weights['variation'], loss_weights['ms_ssim'],
        0., 0., 0.,
        statistics['track_mean'], statistics['track_variation'], statistics['track_ms_ssim'],
        statistics['track_difference_histogram'], statistics['track_variation_difference_histogram'],
        0., 0., 0.,
        0., 0.)
  
  
  neural_network = NeuralNetwork(
      architecture=architecture, number_of_filters_for_convolution_blocks=number_of_filters_for_convolution_blocks,
      number_of_convolutions_per_block=number_of_convolutions_per_block, use_batch_normalization=use_batch_normalization,
      dropout_rate=dropout_rate, use_single_feature_prediction=use_single_feature_prediction,
      feature_flags=feature_flags, use_multiscale_predictions=use_multiscale_predictions,
      invert_standardization_after_multiscale_predictions=invert_standardization_after_multiscale_predictions,
      use_kernel_predicion=use_kernel_predicion, kernel_size=kernel_size)
  
  
  # TODO: CPU only has to be configurable. (DeepBlender)

  use_XLA = True
  use_CPU_only = False
  
  run_config = None
  if use_XLA:
    if use_CPU_only:
      session_config = tf.ConfigProto(device_count = {'GPU': 0})
    else:
      session_config = tf.ConfigProto()
    session_config.graph_options.optimizer_options.global_jit_level = tf.OptimizerOptions.ON_1
    save_summary_steps = 100
    save_checkpoints_step = 500
    run_config = tf.estimator.RunConfig(
        session_config=session_config, save_summary_steps=save_summary_steps,
        save_checkpoints_steps=save_checkpoints_step)
  
  estimator = tf.estimator.Estimator(
      model_fn=model_fn,
      model_dir=model_directory,
      config=run_config,
      params={
          'prediction_features': prediction_features,
          'use_CPU_only': use_CPU_only,
          'data_format': parsed_arguments.data_format,
          'learning_rate': learning_rate,
          'batch_size': batch_size,
          'neural_network': neural_network,
          'use_multiscale_loss': use_multiscale_loss,
          'use_multiscale_metrics': use_multiscale_metrics,
          'training_features': training_features,
          'combined_training_features': combined_training_features,
          'combined_image_training_feature': combined_image_training_feature})
  
  if parsed_arguments.validate:
    index_tuples, required_indices = source_index_tuples(
        validation_number_of_sources_per_example, number_of_source_index_tuples, number_of_sources_per_target)
    evaluate(validation_tfrecords_directory, estimator, training_features_loader, training_features_augmentation,
        index_tuples, required_indices, data_augmentation_usage, training_tiles_height_width,
        batch_size, parsed_arguments.threads)
  else:
    remaining_number_of_epochs = parsed_arguments.train_epochs
    while remaining_number_of_epochs > 0:
      number_of_training_epochs = parsed_arguments.validation_interval
      if remaining_number_of_epochs < number_of_training_epochs:
        number_of_training_epochs = remaining_number_of_epochs
      
      for _ in range(number_of_training_epochs):
        epochs_to_train = 1
        index_tuples, required_indices = source_index_tuples(
            training_number_of_sources_per_example, number_of_source_index_tuples, number_of_sources_per_target)
        train(
            training_tfrecords_directory, estimator, training_features_loader, training_features_augmentation,
            epochs_to_train, index_tuples, required_indices, data_augmentation_usage, training_tiles_height_width,
            batch_size, parsed_arguments.threads)
      
      index_tuples, required_indices = source_index_tuples(
          validation_number_of_sources_per_example, number_of_source_index_tuples, number_of_sources_per_target)
      evaluate(validation_tfrecords_directory, estimator, training_features_loader, training_features_augmentation,
          index_tuples, required_indices, data_augmentation_usage, training_tiles_height_width,
          batch_size, parsed_arguments.threads)
      
      remaining_number_of_epochs = remaining_number_of_epochs - number_of_training_epochs


if __name__ == '__main__':
  parsed_arguments, unparsed = parser.parse_known_args()
  main(parsed_arguments)