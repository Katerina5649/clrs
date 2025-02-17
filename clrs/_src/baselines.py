# Copyright 2021 DeepMind Technologies Limited. All Rights Reserved.
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

"""JAX implementation of CLRS baseline models."""

import functools
import os
import pickle

from typing import Dict, Tuple

import chex

from clrs._src import decoders
from clrs._src import losses
from clrs._src import model
from clrs._src import nets
from clrs._src import probing
from clrs._src import samplers
from clrs._src import specs

import haiku as hk
import jax
import jax.numpy as jnp
import optax


_Array = chex.Array
_DataPoint = probing.DataPoint
_Features = samplers.Features
_FeaturesChunked = samplers.FeaturesChunked
_Feedback = samplers.Feedback
_Location = specs.Location
_Seed = jnp.ndarray
_Spec = specs.Spec
_Stage = specs.Stage
_Trajectory = samplers.Trajectory
_Type = specs.Type
_OutputClass = specs.OutputClass


class BaselineModel(model.Model):
  """Model implementation with selectable message passing algorithm."""

  def __init__(
      self,
      spec,
      nb_heads=1,
      hidden_dim=32,
      kind='mpnn',
      encode_hints=False,
      decode_hints=True,
      decode_diffs=False,
      use_lstm=False,
      learning_rate=0.005,
      checkpoint_path='/tmp/clrs3',
      freeze_processor=False,
      dummy_trajectory=None,
      dropout_prob=0.0,
      name='base_model',
  ):
    super(BaselineModel, self).__init__(spec=spec)

    if encode_hints and not decode_hints:
      raise ValueError('`encode_hints=True`, `decode_hints=False` is invalid.')

    self.spec = spec
    self.decode_hints = decode_hints
    self.decode_diffs = decode_diffs
    self.checkpoint_path = checkpoint_path
    self.name = name
    self._freeze_processor = freeze_processor
    self.opt = optax.adam(learning_rate)

    self.nb_dims = {}
    for inp in dummy_trajectory.features.inputs:
      self.nb_dims[inp.name] = inp.data.shape[-1]
    for hint in dummy_trajectory.features.hints:
      self.nb_dims[hint.name] = hint.data.shape[-1]
    for outp in dummy_trajectory.outputs:
      self.nb_dims[outp.name] = outp.data.shape[-1]

    self._create_net_fns(hidden_dim, encode_hints, kind,
                         use_lstm, dropout_prob, nb_heads)
    self.params = None
    self.opt_state = None

  def _create_net_fns(self, hidden_dim, encode_hints, kind,
                      use_lstm, dropout_prob, nb_heads):
    def _use_net(*args, **kwargs):
      return nets.Net(self.spec, hidden_dim, encode_hints,
                      self.decode_hints, self.decode_diffs,
                      kind, use_lstm, dropout_prob,
                      nb_heads, self.nb_dims)(*args, **kwargs)

    self.net_fn = hk.transform(_use_net)
    self.net_fn_apply = jax.jit(self.net_fn.apply, static_argnums=3)

  def init(self, features: _Features, seed: _Seed):
    self.params = self.net_fn.init(jax.random.PRNGKey(seed), features, True)
    self.opt_state = self.opt.init(self.params)

  def feedback(self, rng_key: hk.PRNGSequence, feedback: _Feedback) -> float:
    """Advance to the next task, incorporating any available feedback."""
    self.params, self.opt_state, cur_loss = self.update(
        rng_key, self.params, self.opt_state, feedback)
    return cur_loss

  def predict(self, rng_key: hk.PRNGSequence, features: _Features):
    """Model inference step."""
    outs, hint_preds, diff_logits, gt_diff = self.net_fn_apply(
        self.params, rng_key, features, True)
    return decoders.postprocess(self.spec,
                                outs), (hint_preds, diff_logits, gt_diff)

  def update(
      self,
      rng_key: hk.PRNGSequence,
      params: hk.Params,
      opt_state: optax.OptState,
      feedback: _Feedback,
  ) -> Tuple[hk.Params, optax.OptState, _Array]:
    """Model update step."""

    def loss(params, rng_key, feedback):
      """Calculates model loss f(feedback; params)."""
      (output_preds, hint_preds, diff_logits,
       gt_diffs) = self.net_fn_apply(params, rng_key, feedback.features, False)

      nb_nodes = _nb_nodes(feedback, is_chunked=False)
      lengths = feedback.features.lengths
      total_loss = 0.0

      # Calculate output loss.
      for truth in feedback.outputs:
        total_loss += losses.output_loss(
            truth=truth,
            pred=output_preds[truth.name],
            nb_nodes=nb_nodes,
        )

      # Optionally accumulate diff losses.
      if self.decode_diffs:
        total_loss += losses.diff_loss(
            diff_logits=diff_logits,
            gt_diffs=gt_diffs,
            lengths=lengths,
        )

      # Optionally accumulate hint losses.
      if self.decode_hints:
        for truth in feedback.features.hints:
          total_loss += losses.hint_loss(
              truth=truth,
              preds=[x[truth.name] for x in hint_preds],
              gt_diffs=gt_diffs,
              lengths=lengths,
              nb_nodes=nb_nodes,
              decode_diffs=self.decode_diffs,
          )

      return total_loss

    # Calculate and apply gradients.
    lss, grads = jax.value_and_grad(loss)(params, rng_key, feedback)
    new_params, opt_state = self._update_params(params, grads, opt_state)

    return new_params, opt_state, lss

  def _update_params(self, params, grads, opt_state):
    updates, opt_state = self.opt.update(grads, opt_state)
    if self._freeze_processor:
      params_subset = _filter_processor(params)
      updates_subset = _filter_processor(updates)
      new_params = optax.apply_updates(params_subset, updates_subset)
      new_params = hk.data_structures.merge(params, new_params)
    else:
      new_params = optax.apply_updates(params, updates)

    return new_params, opt_state

  def verbose_loss(self, feedback: _Feedback, extra_info) -> Dict[str, _Array]:
    """Gets verbose loss information."""
    hint_preds, diff_logits, gt_diffs = extra_info

    nb_nodes = _nb_nodes(feedback, is_chunked=False)
    lengths = feedback.features.lengths
    losses_ = {}

    # Optionally accumulate diff losses.
    if self.decode_diffs:
      losses_.update(
          losses.diff_loss(
              diff_logits=diff_logits,
              gt_diffs=gt_diffs,
              lengths=lengths,
              verbose=True,
          ))

    # Optionally accumulate hint losses.
    if self.decode_hints:
      for truth in feedback.features.hints:
        losses_.update(
            losses.hint_loss(
                truth=truth,
                preds=hint_preds,
                gt_diffs=gt_diffs,
                lengths=lengths,
                nb_nodes=nb_nodes,
                decode_diffs=self.decode_diffs,
                verbose=True,
            ))

    return losses_

  def restore_model(self, file_name: str, only_load_processor: bool = False):
    """Restore model from `file_name`."""
    path = os.path.join(self.checkpoint_path, file_name)
    with open(path, 'rb') as f:
      restored_state = pickle.load(f)
      if only_load_processor:
        restored_params = _filter_processor(restored_state['params'])
      else:
        restored_params = restored_state['params']
      self.params = hk.data_structures.merge(self.params, restored_params)
      self.opt_state = restored_state['opt_state']

  def save_model(self, file_name: str):
    """Save model (processor weights only) to `file_name`."""
    os.makedirs(self.checkpoint_path, exist_ok=True)
    to_save = {'params': self.params, 'opt_state': self.opt_state}
    path = os.path.join(self.checkpoint_path, file_name)
    with open(path, 'wb') as f:
      pickle.dump(to_save, f)


class BaselineModelChunked(BaselineModel):
  """Model that processes time-chunked data.

    Unlike `BaselineModel`, which processes full samples, `BaselineModelChunked`
    processes fixed-timelength chunks of data. Each tensor of inputs and hints
    has dimensions chunk_length x batch_size x ... The beginning of a new
    sample withing the chunk is signalled by a tensor called `is_first` of
    dimensions chunk_length x batch_size.

    The chunked model is intended for training. For validation and test, use
    `BaselineModel`.
  """

  def _create_net_fns(self, hidden_dim, encode_hints, kind,
                      use_lstm, dropout_prob, nb_heads):
    def _use_net(*args, **kwargs):
      return nets.NetChunked(
          self.spec, hidden_dim, encode_hints,
          self.decode_hints, self.decode_diffs,
          kind, use_lstm, dropout_prob,
          nb_heads, self.nb_dims)(*args, **kwargs)

    self.net_fn = hk.transform(_use_net)
    self.net_fn_apply = jax.jit(
        functools.partial(self.net_fn.apply, init_mp_state=False),
        static_argnums=4)

  def _init_mp_state(self, features: _FeaturesChunked, rng_key: _Array):
    empty_mp_state = nets.MessagePassingStateChunked(
        inputs=None, hints=None, is_first=None,
        hint_preds=None, hiddens=None, lstm_state=None)
    dummy_params = self.net_fn.init(
        rng_key, features, empty_mp_state, False, init_mp_state=True)
    _, mp_state = self.net_fn.apply(
        dummy_params, rng_key, features, empty_mp_state, False,
        init_mp_state=True)
    return mp_state

  def init(self, features: _FeaturesChunked, seed: _Seed):
    self.mp_state = self._init_mp_state(features, jax.random.PRNGKey(seed))
    self.params = self.net_fn.init(
        jax.random.PRNGKey(seed), features, self.mp_state,
        True, init_mp_state=False)
    self.opt_state = self.opt.init(self.params)

  def predict(self, rng_key: hk.PRNGSequence, features: _FeaturesChunked):
    """Inference not implemented. Chunked model intended for training only."""
    raise NotImplementedError

  def update(
      self,
      rng_key: hk.PRNGSequence,
      params: hk.Params,
      opt_state: optax.OptState,
      feedback: _Feedback,
  ) -> Tuple[hk.Params, optax.OptState, _Array]:
    """Model update step."""

    def loss(params, rng_key, feedback):
      ((output_preds, hint_preds, diff_logits, gt_diffs),
       mp_state) = self.net_fn_apply(params, rng_key, feedback.features,
                                     self.mp_state, False)

      nb_nodes = _nb_nodes(feedback, is_chunked=True)

      total_loss = 0.0
      is_first = feedback.features.is_first
      is_last = feedback.features.is_last

      # Calculate output loss.
      for truth in feedback.outputs:
        total_loss += losses.output_loss_chunked(
            truth=truth,
            pred=output_preds[truth.name],
            is_last=is_last,
            nb_nodes=nb_nodes,
        )

      # Optionally accumulate diff losses.
      if self.decode_diffs:
        total_loss += losses.diff_loss_chunked(
            diff_logits=diff_logits,
            gt_diffs=gt_diffs,
            is_first=is_first,
        )

      # Optionally accumulate hint losses.
      if self.decode_hints:
        for truth in feedback.features.hints:
          loss = losses.hint_loss_chunked(
              truth=truth,
              pred=hint_preds[truth.name],
              gt_diffs=gt_diffs,
              is_first=is_first,
              nb_nodes=nb_nodes,
              decode_diffs=self.decode_diffs,
          )
          total_loss += loss

      return total_loss, (mp_state,)

    (lss, (self.mp_state,)), grads = jax.value_and_grad(
        loss, has_aux=True)(params, rng_key, feedback)
    new_params, opt_state = self._update_params(params, grads, opt_state)

    return new_params, opt_state, lss

  def verbose_loss(self, *args, **kwargs):
    raise NotImplementedError


def _nb_nodes(feedback: _Feedback, is_chunked) -> int:
  for inp in feedback.features.inputs:
    if inp.location in [_Location.NODE, _Location.EDGE]:
      if is_chunked:
        return inp.data.shape[2]  # inputs are time x batch x nodes x ...
      else:
        return inp.data.shape[1]  # inputs are batch x nodes x ...
  assert False


def _filter_processor(params: hk.Params) -> hk.Params:
  return hk.data_structures.filter(
      lambda module_name, n, v: 'construct_processor' in module_name, params)


def _is_not_done_broadcast(lengths, i, tensor):
  is_not_done = (lengths > i + 1) * 1.0
  while len(is_not_done.shape) < len(tensor.shape):
    is_not_done = jnp.expand_dims(is_not_done, -1)
  return is_not_done
