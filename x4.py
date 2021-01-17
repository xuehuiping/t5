from __future__ import print_function

# -*- encoding: utf-8 -*-
"""
@date: 2021/1/17 10:06 上午
@author: xuehuiping
"""
'''
使用法研杯摘要数据进行训练
'''

import json
import random
import numpy as np
from tqdm import tqdm
from bert4keras.backend import keras, K
from bert4keras.layers import Loss
from bert4keras.models import build_transformer_model
from bert4keras.tokenizers import SpTokenizer
from bert4keras.optimizers import Adam
from bert4keras.snippets import sequence_padding, open
from bert4keras.snippets import DataGenerator, AutoRegressiveDecoder
from keras.models import Model
from rouge import Rouge  # pip install rouge
from nltk.translate.bleu_score import sentence_bleu, SmoothingFunction

# 基本参数
max_c_len = 256
max_t_len = 32
batch_size = 16
epochs = 40

# 模型路径
model_path = '/Users/xuehuiping/data/mt5/mt5_base/'
config_path = '/Users/xuehuiping/data/mt5/mt5_base_config.json'
checkpoint_path = model_path + 'model.ckpt-1000000'
spm_path = '/Users/xuehuiping/data/mt5/sentencepiece_cn.model'
keep_tokens_path = '/Users/xuehuiping/data/mt5/sentencepiece_cn_keep_tokens.json'

# 训练的模型的保存路径
model_path = '.results/best_model.weights'


def load_data2(filename):
    """加载数据
    返回：[(text, summary)]
    """
    D = []
    with open(filename, encoding='utf-8') as f:
        for l in f:
            l = json.loads(l)
            text = '\n'.join([d['sentence'] for d in l['text']])
            D.append((l['summary'], text))
    return D


all_data = load_data2('/Users/xuehuiping/git/SPACES/data/sfzy_small.json')
print(len(all_data))
random.shuffle(all_data)
p1 = (int)(0.8 * len(all_data))
p2 = (int)(0.9 * len(all_data))
train_data = all_data[:p1]
valid_data = all_data[p1:p2]
test_data = all_data[p2:]
print(len(train_data))
print(len(valid_data))
print(len(test_data))

# 加载分词器
tokenizer = SpTokenizer(spm_path, token_start=None, token_end='</s>')
keep_tokens = json.load(open(keep_tokens_path))


class data_generator(DataGenerator):
    """数据生成器
    """

    def __iter__(self, random=False):
        batch_c_token_ids, batch_t_token_ids = [], []
        for is_end, (title, content) in self.sample(random):
            c_token_ids, _ = tokenizer.encode(content, maxlen=max_c_len)
            t_token_ids, _ = tokenizer.encode(title, maxlen=max_t_len)
            batch_c_token_ids.append(c_token_ids)
            batch_t_token_ids.append([0] + t_token_ids)
            if len(batch_c_token_ids) == self.batch_size or is_end:
                batch_c_token_ids = sequence_padding(batch_c_token_ids)
                batch_t_token_ids = sequence_padding(batch_t_token_ids)
                yield [batch_c_token_ids, batch_t_token_ids], None
                batch_c_token_ids, batch_t_token_ids = [], []


class CrossEntropy(Loss):
    """交叉熵作为loss，并mask掉输入部分
    """

    def compute_loss(self, inputs, mask=None):
        y_true, y_pred = inputs
        y_true = y_true[:, 1:]  # 目标token_ids
        y_mask = K.cast(mask[1], K.floatx())[:, :-1]  # 解码器自带mask
        y_pred = y_pred[:, :-1]  # 预测序列，错开一位
        loss = K.sparse_categorical_crossentropy(y_true, y_pred)
        loss = K.sum(loss * y_mask) / K.sum(y_mask)
        return loss


t5 = build_transformer_model(
    config_path=config_path,
    checkpoint_path=checkpoint_path,
    keep_tokens=keep_tokens,
    model='t5.1.1',
    return_keras_model=False,
    name='T5',
)

encoder = t5.encoder
decoder = t5.decoder
model = t5.model
model.summary()

output = CrossEntropy(1)([model.inputs[1], model.outputs[0]])

model = Model(model.inputs, output)
model.compile(optimizer=Adam(2e-4))


class AutoTitle(AutoRegressiveDecoder):
    """seq2seq解码器
    """

    @AutoRegressiveDecoder.wraps(default_rtype='probas')
    def predict(self, inputs, output_ids, states):
        c_encoded = inputs[0]
        return self.last_token(decoder).predict([c_encoded, output_ids])

    def generate(self, text, topk=1):
        c_token_ids, _ = tokenizer.encode(text, maxlen=max_c_len)
        c_encoded = encoder.predict(np.array([c_token_ids]))[0]
        output_ids = self.beam_search([c_encoded], topk=topk)  # 基于beam search
        return tokenizer.decode([int(i) for i in output_ids])


# 注：T5有一个很让人不解的设置，它的<bos>标记id是0，即<bos>和<pad>其实都是0
autotitle = AutoTitle(start_id=0, end_id=tokenizer._token_end_id, maxlen=32)


class Evaluator(keras.callbacks.Callback):
    """评估与保存
    """

    def __init__(self):
        self.rouge = Rouge()
        self.smooth = SmoothingFunction().method1
        self.best_bleu = 0.

    def on_epoch_end(self, epoch, logs=None):
        metrics = self.evaluate(valid_data)  # 评测模型
        if metrics['bleu'] > self.best_bleu:
            self.best_bleu = metrics['bleu']
            model.save_weights(model_path)  # 保存模型
        metrics['best_bleu'] = self.best_bleu
        print('valid_data:', metrics)

    def evaluate(self, data, topk=1):
        total = 0
        rouge_1, rouge_2, rouge_l, bleu = 0, 0, 0, 0
        for title, content in tqdm(data):
            total += 1
            title = ' '.join(title).lower()
            pred_title = ' '.join(autotitle.generate(content,
                                                     topk=topk)).lower()
            if pred_title.strip():
                scores = self.rouge.get_scores(hyps=pred_title, refs=title)
                rouge_1 += scores[0]['rouge-1']['f']
                rouge_2 += scores[0]['rouge-2']['f']
                rouge_l += scores[0]['rouge-l']['f']
                bleu += sentence_bleu(
                    references=[title.split(' ')],
                    hypothesis=pred_title.split(' '),
                    smoothing_function=self.smooth
                )
        rouge_1 /= total
        rouge_2 /= total
        rouge_l /= total
        bleu /= total
        return {
            'rouge-1': rouge_1,
            'rouge-2': rouge_2,
            'rouge-l': rouge_l,
            'bleu': bleu,
        }


if __name__ == '__main__':

    evaluator = Evaluator()
    train_generator = data_generator(train_data, batch_size)

    model.fit(
        train_generator.forfit(),
        steps_per_epoch=len(train_generator),
        epochs=epochs,
        callbacks=[evaluator]
    )

else:

    model.load_weights(model_path)
