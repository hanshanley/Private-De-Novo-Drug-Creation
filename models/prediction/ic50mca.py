# -*- coding: utf-8 -*-
"""Copy of IC50Predictions.ipynb

Automatically generated by Colaboratory.

Multihead convolutional encoder for predicting IC50 value of particular 
chemical compound drugs on particular target RNA sequence. This model 
has a layer that highlights which particular genese were most important 
in predicting the IC50 value. This models takes a subset of 2128 genes as 
defined in  https://pubs.acs.org/doi/pdf/10.1021/acs.molpharmaceut.9b00520.

This is heavily based from https://github.com/drugilsberg/paccmann

"""

import numpy as np
import tensorflow as tf
import tensorflow.keras.backend as K
import tensorflow.keras as keras
import pandas as pd
import math
import tensorflow.keras.layers as layers
import time
import numpy as np
import matplotlib.pyplot as plt

BATCH_NORM = True
CONV_ACTIVATION = 'tanh'
CONV_DEPTH = 4
CONV_DIM_DEPTH = 32
CONV_DIM_WIDTH = 16
CONV_D_GF = 1.15875438383
CONV_W_GF = 1.1758149644
HIDDEN_DIM = 256
HG_GROWTH_FACTOR = 1.4928245388
MIDDLE_LAYERS = 1

def get_angles(pos, i, d_model):
  angle_rates = 1 / np.power(10000, (2 * (i//2)) / np.float32(d_model))
  return pos * angle_rates

def scaled_dot_product_attention(q, k, v, mask):
  """Calculate the attention weights.
  q, k, v must have matching leading dimensions.
  k, v must have matching penultimate dimension, i.e.: seq_len_k = seq_len_v.
  The mask has different shapes depending on its type(padding or look ahead) 
  but it must be broadcastable for addition.
  
  Args:
    q: query shape == (..., seq_len_q, depth)
    k: key shape == (..., seq_len_k, depth)
    v: value shape == (..., seq_len_v, depth_v)
    mask: Float tensor with shape broadcastable 
          to (..., seq_len_q, seq_len_k). Defaults to None.
    
  Returns:
    output, attention_weights
  """

  matmul_qk = tf.matmul(q, k, transpose_b=True)  # (..., seq_len_q, seq_len_k)
  
  # scale matmul_qk
  dk = tf.cast(tf.shape(k)[-1], tf.float32)
  scaled_attention_logits = matmul_qk / tf.math.sqrt(dk)

  # add the mask to the scaled tensor.
  if mask is not None:
    scaled_attention_logits += (mask * -1e9)  

  # softmax is normalized on the last axis (seq_len_k) so that the scores
  # add up to 1.
  attention_weights = tf.nn.softmax(scaled_attention_logits, axis=-1)  # (..., seq_len_q, seq_len_k)

  output = tf.matmul(attention_weights, v)  # (..., seq_len_q, depth_v)

  return output, attention_weights

class MultiHeadAttention(tf.keras.layers.Layer):
  def __init__(self, d_model, num_heads):
    super(MultiHeadAttention, self).__init__()
    self.num_heads = num_heads
    self.d_model = d_model
    
    assert d_model % self.num_heads == 0
    
    self.depth = d_model // self.num_heads
    
    self.wq = tf.keras.layers.Dense(d_model)
    self.wk = tf.keras.layers.Dense(d_model)
    self.wv = tf.keras.layers.Dense(d_model)
    
    self.dense = tf.keras.layers.Dense(d_model)
        
  def split_heads(self, x, batch_size):
    """Split the last dimension into (num_heads, depth).
    Transpose the result such that the shape is (batch_size, num_heads, seq_len, depth)
    """
    x = tf.reshape(x, (batch_size, -1, self.num_heads, self.depth))
    return tf.transpose(x, perm=[0, 2, 1, 3])
    
  def call(self, v, k, q, mask):
    batch_size = tf.shape(q)[0]
    
    q = self.wq(q)  # (batch_size, seq_len, d_model)
    k = self.wk(k)  # (batch_size, seq_len, d_model)
    v = self.wv(v)  # (batch_size, seq_len, d_model)
    
    q = self.split_heads(q, batch_size)  # (batch_size, num_heads, seq_len_q, depth)
    k = self.split_heads(k, batch_size)  # (batch_size, num_heads, seq_len_k, depth)
    v = self.split_heads(v, batch_size)  # (batch_size, num_heads, seq_len_v, depth)
    
    # scaled_attention.shape == (batch_size, num_heads, seq_len_q, depth)
    # attention_weights.shape == (batch_size, num_heads, seq_len_q, seq_len_k)
    scaled_attention, attention_weights = scaled_dot_product_attention(
        q, k, v, mask)
    
    scaled_attention = tf.transpose(scaled_attention, perm=[0, 2, 1, 3])  # (batch_size, seq_len_q, num_heads, depth)

    concat_attention = tf.reshape(scaled_attention, 
                                  (batch_size, -1, self.d_model))  # (batch_size, seq_len_q, d_model)

    output = self.dense(concat_attention)  # (batch_size, seq_len_q, d_model)
        
    return output, attention_weights

def point_wise_feed_forward_network(d_model, dff):
  return tf.keras.Sequential([
      tf.keras.layers.Dense(dff, activation='relu'),  # (batch_size, seq_len, dff)
      tf.keras.layers.Dense(d_model)  # (batch_size, seq_len, d_model)
  ])

def positional_encoding(position, d_model):
  angle_rads = get_angles(np.arange(position)[:, np.newaxis],
                          np.arange(d_model)[np.newaxis, :],
                          d_model)
  
  # apply sin to even indices in the array; 2i
  angle_rads[:, 0::2] = np.sin(angle_rads[:, 0::2])
  
  # apply cos to odd indices in the array; 2i+1
  angle_rads[:, 1::2] = np.cos(angle_rads[:, 1::2])
    
  pos_encoding = angle_rads[np.newaxis, ...]
    
  return tf.cast(pos_encoding, dtype=tf.float32)

class EncoderLayer(tf.keras.layers.Layer):
  def __init__(self, d_model, num_heads, dff, rate=0.1):
    super(EncoderLayer, self).__init__()

    self.mha = MultiHeadAttention(d_model, num_heads)
    self.ffn = point_wise_feed_forward_network(d_model, dff)

    self.layernorm1 = tf.keras.layers.LayerNormalization(epsilon=1e-6)
    self.layernorm2 = tf.keras.layers.LayerNormalization(epsilon=1e-6)
    
    self.dropout1 = tf.keras.layers.Dropout(rate)
    self.dropout2 = tf.keras.layers.Dropout(rate)
    
  def call(self, x, training, mask):

    attn_output, _ = self.mha(x, x, x, mask)  # (batch_size, input_seq_len, d_model)
    attn_output = self.dropout1(attn_output, training=training)
    out1 = self.layernorm1(x + attn_output)  # (batch_size, input_seq_len, d_model)
    
    ffn_output = self.ffn(out1)  # (batch_size, input_seq_len, d_model)
    ffn_output = self.dropout2(ffn_output, training=training)
    out2 = self.layernorm2(out1 + ffn_output)  # (batch_size, input_seq_len, d_model)
    
    return out2

class DecoderLayer(tf.keras.layers.Layer):
  def __init__(self, d_model, num_heads, dff, rate=0.1):
    super(DecoderLayer, self).__init__()

    self.mha1 = MultiHeadAttention(d_model, num_heads)
    self.mha2 = MultiHeadAttention(d_model, num_heads)

    self.ffn = point_wise_feed_forward_network(d_model, dff)
 
    self.layernorm1 = tf.keras.layers.LayerNormalization(epsilon=1e-6)
    self.layernorm2 = tf.keras.layers.LayerNormalization(epsilon=1e-6)
    self.layernorm3 = tf.keras.layers.LayerNormalization(epsilon=1e-6)
    
    self.dropout1 = tf.keras.layers.Dropout(rate)
    self.dropout2 = tf.keras.layers.Dropout(rate)
    self.dropout3 = tf.keras.layers.Dropout(rate)
    
    
  def call(self, x, enc_output, training, 
           look_ahead_mask, padding_mask):
    # enc_output.shape == (batch_size, input_seq_len, d_model)
    attn1, attn_weights_block1 = self.mha1(x, x, x, look_ahead_mask)  # (batch_size, target_seq_len, d_model)
    attn1 = self.dropout1(attn1, training=training)
    out1 = self.layernorm1(attn1 + x)
    
    attn2, attn_weights_block2 = self.mha2(
        enc_output, enc_output, out1, padding_mask)  # (batch_size, target_seq_len, d_model)
    attn2 = self.dropout2(attn2, training=training)
    out2 = self.layernorm2(attn2 + out1)  # (batch_size, target_seq_len, d_model)
    
    ffn_output = self.ffn(out2)  # (batch_size, target_seq_len, d_model)
    ffn_output = self.dropout3(ffn_output, training=training)
    out3 = self.layernorm3(ffn_output + out2)  # (batch_size, target_seq_len, d_model)
    
    return out3, attn_weights_block1, attn_weights_block2

class ContextualAttentionLayer(tf.keras.layers.Layer):
  def __init__(self, attention_size=256,num_genes =2128, hidden_dim=512, name=None):
    super(ContextualAttentionLayer, self).__init__()
    self.w_num_gene_features = tf.Variable(
        tf.keras.backend.random_normal([1], stddev=0.1)
    )
    self.w_genes = tf.Variable(
        tf.keras.backend.random_normal([num_genes, attention_size], stddev=0.1)
    )
    self.b_genes = tf.Variable(tf.keras.backend.random_normal([attention_size], stddev=0.1))
    self.dense_smiles = tf.keras.layers.Dense(attention_size)
    self.v =  tf.Variable(tf.keras.backend.random_normal([attention_size], stddev=0.1))

  def call(self,genes, smiles,reduce_sequence=True,return_alphas=True,):
    genes = tf.expand_dims(genes, 2)
    genes_collapsed = tf.tensordot(
        genes, self.w_num_gene_features, axes=[2, 0]
    )
    x = tf.tanh(
            tf.expand_dims(
                tf.tensordot(
                    genes_collapsed, self.w_genes, axes=1
                ) + self.b_genes,
                axis=1
            ) 
            + self.dense_smiles(smiles)
    )

    # For each of the timestamps its vector of size attention_size
    # from `v` is reduced with `u` vector
    # `[batch_size, sequence_length]`
    xv = tf.tensordot(x, self.v, axes=1, name='unnormalized')
    # `[batch_size, sequence_length]`
    alphas = tf.nn.softmax(xv, name='alphas')

    # If reduce_sequence is true, result is `[batch_size, hidden_size]`
    # else it is `[batch_size, sequence_length, hidden_size]`
    output = (
        tf.reduce_sum(smiles * tf.expand_dims(alphas, -1), 1)
        if reduce_sequence else
        smiles * tf.expand_dims(alphas, -1)
    )
    # Optionally return the attention weights
    return (
        (output, alphas)
        if return_alphas else
        output
    )

def contextual_attention_layer(
    genes, smiles, attention_size, reduce_sequence=True,
    return_alphas=True, name=None):
    """
    Inspired by Bahdanau attention, this layer implements an layer that defines
    for each token of the encoded SMILES
    (e.g. bRNN, raw embedding, conv_output) how well it targets the genes. 
    Args:
        - genes: this must be a `tf.Tensor` of shape:
            `[batch_size, num_genes]` or shape
            `[batch_size, num_genes, num_gene_features]`
            e.g. num_gene_features = 5 if copy number variation data is used.
        - smiles: encoded smiles. This must be a `tf.Tensor` of shape:
            `[batch_size, sequence_length, hidden_size]`
        - attention_size: amount of attention units (<int>).
        - reduce_sequence: whether the sequence_length dim is reduced (<bool>).
        - return_alphas: whether the attention weights are returned (<bool>).
    Returns:
        - If reduce_sequence == True (default), return will be a `tf.Tensor`
            shaped `[batch_size, hidden_size]`, else
            `[batch_size, sequence_length, hidden_size]`.
        - If return_alphas == True, return will be a tuple of 2 `tf.Tensor`,
            the first as the attention output and the second as the attention
            weights (`[batch_size, sequence_length]`).
    """
    genes = tf.expand_dims(genes, 2) if len(genes.shape) == 2 else genes
    hidden_size = smiles.shape[2].value
    num_genes = genes.shape[1].value
    num_gene_features = genes.shape[2].value

    # Trainable parameters.
    w_num_gene_features = tf.Variable(
        tf.random_normal([num_gene_features], stddev=0.1)
    )
    w_genes = tf.Variable(
        tf.random_normal([num_genes, attention_size], stddev=0.1)
    )
    b_genes = tf.Variable(tf.random_normal([attention_size], stddev=0.1))

    
    w_smiles = tf.Variable(
        tf.random_normal([hidden_size, attention_size], stddev=0.1)
    )
    b_smiles = tf.Variable(tf.random_normal([attention_size], stddev=0.1))
    v = tf.Variable(tf.random_normal([attention_size], stddev=0.1))

    # Applying fully connected layer with non-linear activation and
    # genes context to each of the batch_size * sequence_length.
    # Shape of `x` is `[batch_size, sequence_length, attention_size]`

    genes_collapsed = tf.tensordot(
        genes, w_num_gene_features, axes=[2, 0]
    )

    x = tf.tanh(
            tf.expand_dims(
                tf.tensordot(
                    genes_collapsed, w_genes, axes=1
                ) + b_genes,
                axis=1
            ) 
            + (tf.tensordot(smiles, w_smiles, axes=1) + b_smiles)
    )

    # For each of the timestamps its vector of size attention_size
    # from `v` is reduced with `u` vector
    # `[batch_size, sequence_length]`
    xv = tf.tensordot(x, v, axes=1, name='unnormalized')
    # `[batch_size, sequence_length]`
    alphas = tf.nn.softmax(xv, name='alphas')

    # If reduce_sequence is true, result is `[batch_size, hidden_size]`
    # else it is `[batch_size, sequence_length, hidden_size]`
    output = (
        tf.reduce_sum(smiles * tf.expand_dims(alphas, -1), 1)
        if reduce_sequence else
        smiles * tf.expand_dims(alphas, -1)
    )

    # Optionally return the attention weights
    return (
        (output, alphas)
        if return_alphas else
        output
    )


def contextual_attention_matrix_layer(
    genes, smiles,
    return_scores=False, name=None):
    """
    Modifies general/multiplicative attention as defined by Luong. Computes
    a score matrix between genes and smiles, filters both with their 
    respective attention weights and returns a joint feature vector.
    Args:
        - genes: this must be a `tf.Tensor` that can be of shape:
            `[batch_size, num_genes]` or
            `[batch_size, num_genes, num_gene_features]`
            num_gene_features=1 if only transcriptomic data
            (gene expression profiles).
            are used, but num_gene_features=5 if genomic data
            (copy number variation) is also used.
        - smiles: encoded smiles. This must be a `tf.Tensor` of shape:
            `[batch_size, sequence_length, hidden_size]`.
        - return_scores: whether the unnormalized attention matrix
            is returned (<bool>).
    Returns:
        - If return_scores = False (default), return will be a
            `tf.Tensor` of shape
            `[batch_size, hidden_size + num_gene_features]`.
        - If return_scores = True, return will be two `tf.Tensor`, the second 
            carrying the unnormalized attention weights of shape 
            `[batch_size, num_genes, sequence_length]).
    NOTE: To get the molecular attention, collapse num_genes of returned
        scores, then apply softmax. Preferentially, merge across multiheads
        (and conv kernel sizes) to get final distribution.
    """

    hidden_size = smiles.shape[2].value
    genes = tf.expand_dims(genes, 2) if len(genes.shape) == 2 else genes
    num_gene_features = genes.shape[2].value

    # cnv features treated like hidden dimension of input sequence.
    w = tf.Variable(tf.random_normal(
        [num_gene_features, hidden_size], stddev=0.1)
    )
    
    # Luong general attention. See: https://arxiv.org/pdf/1508.04025.pdf.
    # Scores has shape `[batch_size, num_genes, sequence_length]`.
    scores = tf.tanh(
        tf.matmul(
            # This has shape `[batch_size, num_genes, hidden_size]`
            tf.tensordot(genes, w, axes=(2, 0)),
            tf.transpose(smiles, (0, 2, 1))
        ), name='attention_scores'
    )
    
    # Shapes `[batch_size, sequence_length]` and `[batch_size, num_genes]`
    # respectively.
    alpha_smiles = tf.nn.softmax(
        tf.reduce_sum(scores, axis=1),
        axis=1, name='alpha_smiles'
    )
    alpha_genes = tf.nn.softmax(
        tf.reduce_sum(scores, axis=2),
        axis=1, name='alpha_genes'
    )
    filtered_smiles = tf.reduce_sum(
        smiles * tf.expand_dims(alpha_smiles, -1),
        axis=1, name='filtered_smiles'
    )
    filtered_genes= tf.reduce_sum(
        genes * tf.expand_dims(alpha_genes, -1),
        axis=1, name='filtered_genes'
    )
    outputs = tf.concat([
        filtered_smiles, filtered_genes],
        axis=1, name='outputs'
    )

    # Optionally return the attention weights.
    return (
        (outputs, scores)
        if return_scores else
        outputs
    )

class DenseAttentionLayer(tf.keras.layers.Layer):
  def __init__(self,feature_size):
    super(DenseAttentionLayer, self).__init__()
    self.dense1 = layers.Dense(feature_size,activation='softmax')

  def call(self,x,return_alphas = True):
    alphas = self.dense1(x)
    output = tf.multiply(x, alphas, name='filtered_with_attention')
    return (
      (output, alphas)
      if return_alphas else
      output
    )

def dense_attention_layer(inputs, return_alphas=False, name=None):
  """
  Attention mechanism layer for dense inputs.
  Args:
      - inputs: attention inputs. This must be a `tf.Tensor` of shape:
      `[batch_size, feature_size]` or
      `[batch_size, feature_size, hidden_size]`.
      - return_alphas: whether to return attention coefficients variable
        along with layer's output. Used for visualization purpose.
  Returns:
      If return_alphas == False (default) this will be a `tf.Tensor` with
      shape: `[batch_size, feature_size]` else it will be a tuple
      (outputs, alphas) with the alphas being of shape
      `[batch_size, feature_size]`.
  """
  # If input comes with a hidden dimension (e.g. 5 features per gene)
  if len(inputs.shape) == 3:
      inputs = tf.squeeze(
          tf.layers.dense(
              inputs, 1, activation=tf.nn.relu, name='feature_collapse'
          ),
          axis=2
      )
  assert len(inputs.shape)==2
  feature_size = inputs.shape[1].value
  alphas = tf.layers.dense(
      inputs, feature_size,
      activation=tf.nn.softmax,
      name='attention'
  )
  output = tf.multiply(inputs, alphas, name='filtered_with_attention')

  return (
      (output, alphas)
      if return_alphas else
      output
  )

class Encoder(tf.keras.layers.Layer):
  def __init__(self, vocab_size, embedding_dim, 
               max_len, latent_dim,
               recurrent_dropout =0.2,
               dropout_rate=0.2,
               epsilon_std = 1.0):
    super(Encoder, self).__init__()

    self.lstm1 = tf.keras.layers.LSTM(256,return_sequences = False)
    self.latent_dim = latent_dim
    self.epsilon_std = epsilon_std
    self.drop1 = tf.keras.layers.Dropout(dropout_rate)
    self.drop2 = tf.keras.layers.Dropout(dropout_rate)
    self.mean = tf.keras.layers.Dense(latent_dim)
    self.log_var = tf.keras.layers.Dense(latent_dim)
    self.embed =  keras.layers.Embedding(input_dim=vocab_size, 
                                         output_dim=embedding_dim,
                                         mask_zero = True, 
                                embeddings_initializer='random_normal',
                                input_length=max_len,
                                trainable=True)
    self.conv1 = layers.Conv1D(int(CONV_DIM_DEPTH *CONV_D_GF),int(CONV_DIM_WIDTH*CONV_W_GF),
                         activation ='tanh')
    self.conv_layers =  [layers.Conv1D(int(CONV_DIM_DEPTH *CONV_D_GF**j),int(CONV_DIM_WIDTH*CONV_W_GF**j),
                         activation ='tanh') for j in  range(1,CONV_DEPTH-1) ]
    self.dense1 = layers.Dense(latent_dim*4)                                                    
    
  def call(self, x):
    x = self.embed(x)
    x = self.conv1(x)
    x =  layers.BatchNormalization(axis = -1)(x)
    for i in range(len(self.conv_layers)):
      x = self.conv_layers[i](x)
    x =  layers.BatchNormalization(axis = -1)(x)
    x = layers.Flatten()(x)

    x = self.dense1(x)
    x = self.drop1(x)
    x =  layers.BatchNormalization(axis = -1)(x)
    z_mean = self.mean(x)
    z_log_var = self.log_var(x)
    return x, z_mean, z_log_var

  def sample(self,z):
    z_mean,z_log_var = z
    batch_size = z_mean.shape[0]
    epsilon = K.random_normal(shape=(batch_size, self.latent_dim), mean=0.,
                              stddev=self.epsilon_std)
    return z_mean + K.exp(z_log_var/2)*epsilon

class IC50_MCA(tf.keras.Model):
  def __init__(self, vocab_size, num_genes,
               embedding_dim,
               hidden_dim, max_len, latent_dim,
               recurrent_dropout =0.2,
               dropout_rate=0.2, epsilon_std = 1.0):
    super(IC50_MCA, self).__init__()

    self.hidden_dim = hidden_dim
    self.latent_dim = latent_dim
    self.rv = tf.keras.layers.RepeatVector(max_len-1)

    self.cal =  ContextualAttentionLayer(hidden_dim= hidden_dim+latent_dim*4)
    self.drop2 = layers.Dropout(dropout_rate)
    self.drop3 = layers.Dropout(dropout_rate)
    self.dense1 = layers.Dense(512*4,activation='relu')
    self.dal =  DenseAttentionLayer(num_genes)
    self.dense2 = layers.Dense(512*2,activation='relu')
    self.dense3 = layers.Dense(512,activation='relu')
    self.dense4 = layers.Dense(1)

  def call(self, encoded_smiles, genes):

    ## Encode smiles
    smiles_t = self.rv(encoded_smiles)
    smiles_inp = self.lstm1(smiles_t)

    ## encode genes
    genes_t, genes_attention_t = self.dal(genes)

    ## Get smiles context and the alphas that instruct which 
    ## genese are important 
    smiles_context,smiles_context_alphas = self.cal(genes = genes, smiles = smiles_inp)
    dec_input = tf.concat([smiles_context,encoded_smiles,genes_t,genes],axis =1)

    out = self.drop2(self.dense1(dec_input))
    out = self.drop3(self.dense2(out))
    out = self.drop3(self.dense3(out))
    out = self.dense4(out)
    return out