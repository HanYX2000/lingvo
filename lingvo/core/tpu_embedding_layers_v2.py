# Copyright 2023 The TensorFlow Authors. All Rights Reserved.
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
"""A lingvo interface into the Tensorflow TPUEmbedding V2 library.

Overview:
  The TPUEmbeddingLayer defined below grants Lingvo model authors access to the
  TensorFlow TPUEmbedding API to train extra-large embedding tables. Similar to
  other Embedding layers in Lingvo, the interface provided is an `EmbLookup`
  function, which may be called from an FProp block to fetch embedding
  activations for particular batches of ids. This library's advantage is that
  it takes advantage of a TPU coprocessor that permits embeddings to be sharded
  across the HBM of large TPU topologies, and thus can vastly increase the total
  size of the embedding tables one can train.

Restrictions:
  Use of this V2 Layer *requires* TPUStrategy, and thus also the Lingvo
  HostDrivenExecutor (and associated Programs).

See also:
  www.tensorflow.org/api_docs/python/tf/tpu/experimental/embedding/TPUEmbedding
"""
import abc
import collections

from typing import Callable, FrozenSet, List, Set, Optional, Tuple, Union
from typing import Dict

import lingvo.compat as tf
from lingvo.core import base_layer
from lingvo.core import hyperparams
from lingvo.core import py_utils
from lingvo.core import schedule as schedule_lib
from lingvo.core import tpu_embedding_layers

# pylint:disable=g-direct-tensorflow-import
from tensorflow.python.tpu import tpu_embedding_v2
from tensorflow.python.tpu import tpu_embedding_v2_utils
# pylint:enable=g-direct-tensorflow-import


class _TPUEmbeddingOptimizerV2Mixin(
    base_layer.BaseLayer, metaclass=base_layer.ABCLayerMeta
):
  """Defines the inferface for optimizers expected by the V2 Layer below."""

  @abc.abstractmethod
  def CreateOptimizerFn(
      self,
      learning_rate: Union[float, Callable[[], float]],
  ) -> tpu_embedding_v2_utils._Optimizer:  # pylint: disable=protected-access
    """Create TPUEmbedding API optimizer parameters."""


class TPUEmbeddingAdagradOptimizer(
    tpu_embedding_layers.TPUEmbeddingAdagradOptimizer,
    _TPUEmbeddingOptimizerV2Mixin,
):
  """Adagrad optimizer for TPUEmbeddingLayer & TPUEmbeddingTable."""

  def CreateOptimizerFn(
      self, learning_rate: Union[float, Callable[[], float]]
  ) -> tpu_embedding_v2_utils.Adagrad:
    p = self.params

    return tpu_embedding_v2_utils.Adagrad(
        learning_rate=learning_rate,
        initial_accumulator_value=p.initial_accumulator,
        use_gradient_accumulation=p.use_gradient_accumulation,
        weight_decay_factor=p.weight_decay_factor,
        clip_weight_min=p.clip_weight_min,
        clip_weight_max=p.clip_weight_max,
        multiply_weight_decay_factor_by_learning_rate=(
            p.multiply_weight_decay_factor_by_learning_rate
        ),
        clipvalue=(p.clip_gradient_min, p.clip_gradient_max),
    )


class TPUEmbeddingAdamOptimizer(
    tpu_embedding_layers.TPUEmbeddingAdamOptimizer,
    _TPUEmbeddingOptimizerV2Mixin,
):
  """Adam optimizer for TPUEmbeddingLayer & TPUEmbeddingTable."""

  def CreateOptimizerFn(
      self, learning_rate: Union[float, Callable[[], float]]
  ) -> tpu_embedding_v2_utils.Adam:
    p = self.params
    return tpu_embedding_v2_utils.Adam(
        learning_rate=learning_rate,
        beta_1=p.beta1,
        beta_2=p.beta2,
        epsilon=p.epsilon,
        lazy_adam=p.lazy_adam,
        sum_inside_sqrt=p.sum_inside_sqrt,
        use_gradient_accumulation=p.use_gradient_accumulation,
        clip_weight_min=p.clip_weight_min,
        clip_weight_max=p.clip_weight_max,
        weight_decay_factor=p.weight_decay_factor,
        multiply_weight_decay_factor_by_learning_rate=(
            p.multiply_weight_decay_factor_by_learning_rate
        ),
        clipvalue=(p.clip_gradient_min, p.clip_gradient_max),
    )


class TPUEmbeddingTable(tpu_embedding_layers.TPUEmbeddingTable):
  """An embedding table controlled by TPUEmbeddingLayer.

  Note that all input_keys need to be declared upfront.
  """

  # Note: there are other optimizers implemented by the API, but these two are
  #   the only ones currently needed for now.
  optimizer: Union[TPUEmbeddingAdagradOptimizer, TPUEmbeddingAdamOptimizer]
  schedule: schedule_lib.BaseSchedule

  def __init__(self, params):
    super().__init__(params)
    p = self.params

    def _LearningRateFn():
      lr = self.schedule.Value() * p.learning_rate
      TPU_EMBEDDING_MANAGER.AddSummaryTensor(f'tpu_embedding_lr/{p.name}', lr)
      return lr

    # This is the actual TPUEmbedding API object that TPUEmbeddingTable wraps.
    self._table_config = tpu_embedding_v2_utils.TableConfig(
        vocabulary_size=self._padded_vocab_size,
        dim=p.embedding_dim,
        initializer=None,
        optimizer=self.optimizer.CreateOptimizerFn(_LearningRateFn),
        combiner=p.combiner,
        name=f'{self._table_name}_config',
    )

  @property
  def table_config(self) -> tpu_embedding_v2_utils.TableConfig:
    return self._table_config

  def GetDeviceName(self, host_id):
    """Return device to place sharded variables on."""
    if self.params.is_inference:
      # This is to place variables on the same device as other variables.
      return None
    if self.do_eval:
      raise NotImplementedError(
          'Pending authorship of host-driven eval program'
      )
    else:
      worker = self.cluster.params.worker.name
      return f'{worker}/replica:0/task:{host_id}/device:CPU:0'

  def _SequenceEmbLookup(
      self, dense_ids: tf.Tensor, partition_strategy: str
  ) -> tf.Tensor:
    """Sequence embedding lookup.

    Note that we do not support padding ids in sequence embeddings.

    Args:
      dense_ids: An int Tensor of shape [batch, sequence].
      partition_strategy: See TPUEmbeddingLayer partition_strategy param.

    Returns:
      A float32 activations Tensor of shape
      [batch, max_sequence_length, embedding_dim].
    """
    p = self.params
    embs = tf.nn.embedding_lookup(
        params=self.theta.wm,
        ids=tf.reshape(dense_ids, [-1]),
        partition_strategy=partition_strategy,
    )
    out_shape = tf.concat([tf.shape(dense_ids), [p.embedding_dim]], 0)
    return tf.reshape(embs, out_shape)

  def _CombinerEmbLookup(
      self, sparse_ids: tf.SparseTensor, partition_strategy: str
  ) -> tf.Tensor:
    """Combiner embedding lookup.

    Args:
      sparse_ids: An int SparseTensor of shape [batch, ...].
      partition_strategy: See TPUEmbeddingLayer partition_strategy param.

    Returns:
      A float32 activations Tensor of shape [batch, 1, embedding_dim].
    """
    p = self.params
    embs = tf.nn.embedding_lookup_sparse(
        self.theta.wm,
        sp_ids=sparse_ids,
        sp_weights=None,
        combiner=p.combiner,
        partition_strategy=partition_strategy,
    )
    batch_size = sparse_ids.dense_shape[0]
    # For tf.nn.embedding_lookup_sparse, output.dim0 might be different from
    # sparse_ids.dense_shape.dim0.
    # Explicitly pad results to maintain dim0=batch.
    dim0_padlen = tf.cast(batch_size, tf.int32) - tf.shape(embs)[0]
    embs = tf.pad(embs, [[0, dim0_padlen], [0, 0]])
    # [batch, 1, embedding_dim]
    embs = py_utils.HasShape(embs, [batch_size], ndims=1)
    return tf.expand_dims(embs, 1)

  def CpuEmbLookup(
      self, ids_map: py_utils.NestedMap, partition_strategy: str
  ) -> py_utils.NestedMap:
    """CPU evaluation embedding lookup for dense tensors.

    Args:
      ids_map: A NestedMap of nested `input_key` string -> [batch, sequence] int
        Tensor. For sequence embeddings, -1 is used as a padding id.
        Non-sequence embeddings do not support padded ids.
      partition_strategy: See TPUEmbeddingLayer partition_strategy param.

    Returns:
      An activations NestedMap of nested string -> float32 Tensor.
      For non-sequence embeddings: [batch, 1, embedding_dim]
      For sequence embeddings: [batch, max_sequence_length, embedding_dim]
    """
    # "Sequence embedding": no combiner case.
    if self.max_sequence_length > 0:
      return ids_map.Transform(
          lambda ids: self._SequenceEmbLookup(ids, partition_strategy)
      )

    # Non-"Sequence embedding", combiner case
    def _Lookup(ids):
      # Dense to sparse.
      dense_shape = tf.shape(ids, out_type=tf.int64)
      sample_indices = tf.cast(tf.where(tf.not_equal(ids, -1)), tf.int64)
      embedding_indices = tf.cast(tf.gather_nd(ids, sample_indices), tf.int64)
      # [?, embedding_dim]
      sparse_ids = tf.SparseTensor(
          indices=sample_indices,
          values=embedding_indices,
          dense_shape=dense_shape,
      )
      return self._CombinerEmbLookup(sparse_ids, partition_strategy)

    return ids_map.Transform(_Lookup)


class _TPUEmbeddingManager:
  """Manages a global singleton instance of tpu_embedding_v2.TPUEmbedding."""

  def __init__(self):
    # True when a TPUEmbeddingLayer has initialized this class. Otherwise, all
    # operations pass through. This eliminates the need to conditionally check
    # everywhere in the host driven executor code whether the client model is
    # using this API.
    self.enabled = False
    self._tpu_embedding: tpu_embedding_v2.TPUEmbedding = None
    self._feature_names: FrozenSet[str] = frozenset()
    self._gradient_multiplier_schedule: schedule_lib.BaseSchedule = None
    self._summary_tensors: List[Tuple[str, tf.Tensor, tf.Tensor]] = []
    self._sequence_features: Set[str] = set()

    # Used to cache activations upon each Dequeue, for later Lookup calls.
    self._activations: py_utils.NestedMap = py_utils.NestedMap()

  def __bool__(self):
    """Passes through the indicator expressing whether the v2 API is enabled."""
    return self.enabled

  @property
  def tpu_embedding(self) -> tpu_embedding_v2.TPUEmbedding:
    """The global singleton TPUEmbedding object used by all TPUEmbeddingLayerV2s."""
    return self._tpu_embedding

  @tpu_embedding.setter
  def tpu_embedding(self, tpu_embedding: tpu_embedding_v2.TPUEmbedding):
    if self._tpu_embedding is not None:
      raise ValueError('TPUEmbedding has already been set.')
    self._tpu_embedding = tpu_embedding

  @property
  def feature_names(self) -> FrozenSet[str]:
    """Global singleton set of feature names used across the embedding tables."""
    return self._feature_names

  @feature_names.setter
  def feature_names(self, feature_names: FrozenSet[str]):
    self._feature_names = feature_names

  @property
  def sequence_features(self) -> Set[str]:
    return self._sequence_features

  @sequence_features.setter
  def sequence_features(self, features: Set[str]):
    self._sequence_features = features

  @property
  def gradient_multiplier_schedule(self) -> schedule_lib.BaseSchedule:
    return self._gradient_multiplier_schedule

  @gradient_multiplier_schedule.setter
  def gradient_multiplier_schedule(self, schedule: schedule_lib.BaseSchedule):
    self._gradient_multiplier_schedule = schedule

  def AddSummaryTensor(self, name: str, value: tf.Tensor, weight: float = 1.0):
    self._summary_tensors.append((name, value, tf.convert_to_tensor(weight)))

  @property
  def summary_tensors(self) -> List[Tuple[str, tf.Tensor, tf.Tensor]]:
    """Returns a list of (name, value, weight) tuples for summary."""
    return self._summary_tensors

  def Enqueue(self, batch: py_utils.NestedMap) -> None:
    """Enqueues embedding column ids from input batch to TPU coprocessor.

    Args:
      batch: the input batch containing the id features to enqueue.
    """
    self._activations = py_utils.NestedMap()
    if self.enabled:
      self.tpu_embedding.enqueue(batch)

  def Dequeue(self) -> py_utils.NestedMap:
    """Dequeues embedding column ids from TPU coprocessor.

    This should be called once per training step, and it dequeues all embedding
    tables. For later table lookups, use Lookup below.

    Returns:
      A NestedMap of embedding activations.
    """
    if self.enabled:
      self._activations = self.tpu_embedding.dequeue()
      py_utils.CurrentGradientTape().watch(self._activations)
    return self._activations

  def Lookup(
      self, ids: Optional[py_utils.NestedMap] = None
  ) -> py_utils.NestedMap:
    """Returns the TPUEmbedding activations corresponding to the requested ids.

    TPUEmbedding v1 only permits all activations to be dequeued at once, so we
    cache the output from Dequeue and permit sliced lookups here.

    Args:
      ids: a NestedMap of ids whose corresponding activations are returned.
    """
    if ids and self.enabled:
      return self._activations.GetSlice(
          {*ids.Keys()} & {*self._activations.Keys()}
      )
    return self._activations

  def ApplyGradients(
      self, gradients: py_utils.NestedMap
  ) -> Dict[str, tf.Tensor]:
    """Applies schedule-scaled gradient updates to the embedding variables.

    Args:
      gradients: grads to apply to the embedding variables.

    Returns:
      An eval_metrics dictionary for reporting.
    """
    if not self.enabled:
      return {}

    multiplier = self.gradient_multiplier_schedule.Value()
    scaled_grads = gradients.Transform(lambda g: g * multiplier)
    self.tpu_embedding.apply_gradients(scaled_grads)

    return {
        'tpu_embedding_activation_norm': (
            tf.sqrt(py_utils.SumSquared(self.Lookup().Flatten())),
            tf.constant(1.0),
        ),
        'tpu_embedding_grad_norm': (
            tf.sqrt(py_utils.SumSquared(scaled_grads.Flatten())),
            tf.constant(1.0),
        ),
        'tpu_embedding_gradient_multiplier': (multiplier, tf.constant(1.0)),
    }


TPU_EMBEDDING_MANAGER = _TPUEmbeddingManager()


class TPUEmbeddingLayer(tpu_embedding_layers.TPUEmbeddingLayer):
  """Interface to TPU embedding which uses the TF2 TPUEmbedding API."""

  # Type annotations for lingvo child objects
  tables: List[TPUEmbeddingTable]
  optimizer: _TPUEmbeddingOptimizerV2Mixin
  lr_schedule: schedule_lib.BaseSchedule

  @classmethod
  def Params(cls):
    p = super().Params()
    # We override this parameter so that it has a valid default.
    p.optimizer = TPUEmbeddingAdagradOptimizer.Params()
    return p

  def __init__(self, params):
    super().__init__(params)
    p = self.params

    assert p.tables
    assert p.batch_size > 0
    assert p.name
    assert p.gradient_multiplier_schedule
    assert p.partition_strategy in ('mod', 'div')

    # Stop if a table has no optimizer related parameters and the layer also
    # has no optimizer parameters
    for param_name in ('optimizer', 'learning_rate', 'lr_schedule'):
      table_param_missing = any(
          table_params.Get(param_name) is None for table_params in p.tables
      )
      if not p.Get(param_name) and table_param_missing:
        raise ValueError(
            f'A table is missing {param_name=} parameters, and no layer-level '
            f'{param_name} parameters were given.'
        )
      elif table_param_missing:
        for table_params in p.tables:
          if param_name not in table_params:
            value = p.Get(param_name)
            if isinstance(value, hyperparams.Params):
              value = value.Copy()  # Avoid mutating the original copy.
            table_params.Set(**{param_name: value})

    self.tpu_embedding_manager: _TPUEmbeddingManager = None
    self.CreateChildren('tables', p.tables)
    self.CreateChild('optimizer', p.optimizer)
    self.CreateChild(
        'gradient_multiplier_schedule', p.gradient_multiplier_schedule
    )

  def _CreateLayerVariables(self):
    p = self.params

    if not py_utils.IsTpuTraining(p):
      return

    # Note: Several FeatureConfigs can refer to the same TableConfig.
    feature_config = py_utils.NestedMap()
    sequence_features = []
    # There are two ways to indicate a feature should not be combined (and thus
    # treated as a sequence feature):
    # option 1)
    #   Keep inputs as rank 2 sparse tensors and use max_sequence_length -
    #   it may also be useful to call 'embedding.build(per_replica_batch_size)'
    # option 2)  <- We implement this one for now.
    #   Call expand_dims with axis=-1 on sequence tensors, making them of rank 3
    #   and manually set output_shape to [batch_size, sequence_length].
    for table in self.tables:
      for feature in table.input_keys:
        if table.max_sequence_length > 0:
          sequence_features.append(feature)
          feature_config.Set(
              feature,
              tpu_embedding_v2_utils.FeatureConfig(
                  table=table.table_config,
                  output_shape=[p.batch_size, table.max_sequence_length],
              ),
          )
        else:
          feature_config.Set(
              feature,
              tpu_embedding_v2_utils.FeatureConfig(table=table.table_config),
          )

    if not TPU_EMBEDDING_MANAGER:
      TPU_EMBEDDING_MANAGER.enabled = True
      # Note: this line needs to be in a TPUStrategy scope. (We are in one here,
      #   because this function is called indirectly from
      #   HostDrivenExecutor:__init__.)
      # Note: the dequeued activations are packed in the same structure as the
      #   feature_config we provide here (i.e. a NestedMap in Lingvo).
      TPU_EMBEDDING_MANAGER.tpu_embedding = tpu_embedding_v2.TPUEmbedding(
          feature_config=feature_config,
          optimizer=self.optimizer.CreateOptimizerFn(p.learning_rate),
          pipeline_execution_with_tensor_core=(
              p.pipeline_execution_with_tensor_core
          ),
      )
      TPU_EMBEDDING_MANAGER.gradient_multiplier_schedule = (
          self.gradient_multiplier_schedule
      )
      TPU_EMBEDDING_MANAGER.sequence_features = set(sequence_features)

      feature_names = collections.Counter()
      for table in self.tables:
        feature_names.update(table.input_keys)
      if any(v > 1 for v in feature_names.values()):
        raise ValueError(f'Key used by multiple tables ({feature_names=})')
      TPU_EMBEDDING_MANAGER.feature_names = frozenset(feature_names)

      # Keep the manager as an attribute to ensure the underlying API object is
      #   included in model serialization.
      self.tpu_embedding_manager = TPU_EMBEDDING_MANAGER

  def _CheckIdsMap(self, ids_map: py_utils.NestedMap) -> None:
    """Check that the keys in `ids_map` is valid for embedding lookup."""
    assert isinstance(ids_map, py_utils.NestedMap)
    valid_keys = set()
    for table in self.tables:
      for input_key in table.input_keys:
        valid_keys.add(input_key)
    invalid_keys = set(ids_map.Keys()) - valid_keys
    if invalid_keys:
      raise ValueError(
          f'Invalid input keys: {invalid_keys}. (Valid keys: {valid_keys})'
      )

  def EmbLookup(self, ids_map: py_utils.NestedMap) -> py_utils.NestedMap:
    """Looks up embedding vectors for each entry in dense Tensor ids_map.

    Since the TPUEmbedding is monolithic, and consulted once per FProp/BProp, we
    must centralize the lookup. Thus, for multiple features, we contain them
    into a single-lookup rather than allowing the caller to call Lookup multiple
    times.

    Args:
      ids_map: A NestedMap of nested `input_key` string -> [batch, sequence] int
        Tensor. For sequence embeddings, -1 is used as a padding id.
        Non-sequence embeddings do not support padded ids.

    Returns:
      Activations NestedMap of nested string ->
      For non-sequence embeddings:  [batch, 1, embedding_dim],
      For sequence embeddings: [batch, max_sequence_length, embedding_dim]
      float32 Tensor.
    """
    self._CheckIdsMap(ids_map)
    p = self.params

    # TPU Lookup
    if py_utils.IsTpuTraining(p):
      return self.tpu_embedding_manager.Lookup(ids_map)

    # CPU Lookup
    ret = py_utils.NestedMap()
    for table in self.tables:
      ret.Update(
          table.CpuEmbLookup(
              ids_map.GetSlice({*table.input_keys} & {*ids_map.Keys()}),
              p.partition_strategy,
          )
      )
    return ret
