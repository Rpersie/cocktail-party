import argparse
import os
import shutil
import math
from datetime import datetime

from keras.layers import Dense, BatchNormalization, Activation, Dropout
from keras.models import Sequential, model_from_json

import numpy as np

from mediaio.audio_io import AudioSignal, AudioMixer
from dsp.spectogram import MelConverter


class AudioSourceSeparator:

	def __init__(self):
		pass

	@staticmethod
	def load(model_cache_path, weights_cache_path):
		separator = AudioSourceSeparator()

		with open(model_cache_path, "r") as model_fd:
			separator._model = model_from_json(model_fd.read())

		separator._model.load_weights(weights_cache_path)

		return separator

	def init_model(self, spectogram_size):
		self._model = Sequential()

		self._model.add(Dense(units=512, input_dim=spectogram_size))
		self._model.add(BatchNormalization())
		self._model.add(Activation("relu"))
		self._model.add(Dropout(0.25))

		self._model.add(Dense(units=512))
		self._model.add(BatchNormalization())
		self._model.add(Activation("relu"))
		self._model.add(Dropout(0.25))

		self._model.add(Dense(units=spectogram_size))
		self._model.compile(loss='mean_squared_error', optimizer='adam')

	def train(self, x, y):
		self._model.fit(x, y, batch_size=32, epochs=100, verbose=1)

	def evaluate(self, x, y):
		score = self._model.evaluate(x, y, verbose=1)
		return score

	def predict(self, x):
		y = self._model.predict(x)
		return y

	def dump(self, model_cache_path, weights_cache_path):
		with open(model_cache_path, "w") as model_fd:
			model_fd.write(self._model.to_json())

		self._model.save_weights(weights_cache_path)


def prepare_sample(source_file_path1, source_file_path2, slice_duration_ms=100):
	signal1 = AudioSignal.from_wav_file(source_file_path1)
	signal2 = AudioSignal.from_wav_file(source_file_path2)
	mixed_signal = AudioMixer.mix([signal1, signal2])

	new_signal_length = int(math.ceil(
		float(mixed_signal.get_number_of_samples()) / MelConverter.HOP_LENGTH
	)) * MelConverter.HOP_LENGTH

	signal1.pad_with_zeros(new_signal_length)
	mixed_signal.pad_with_zeros(new_signal_length)

	mel_converter = MelConverter(mixed_signal.get_sample_rate())

	mixed_mel_spectogram = mel_converter.signal_to_mel_spectogram(mixed_signal)
	signal1_mel_spectogram = mel_converter.signal_to_mel_spectogram(signal1)

	samples_per_slice = int((float(slice_duration_ms) / 1000) * mixed_signal.get_sample_rate())
	spectogram_samples_per_slice = int(samples_per_slice / MelConverter.HOP_LENGTH)

	n_slices = int(mixed_mel_spectogram.shape[1] / spectogram_samples_per_slice)

	x = np.ndarray(shape=(n_slices, MelConverter.N_MEL_FREQS * spectogram_samples_per_slice))
	y = np.ndarray(shape=(n_slices, MelConverter.N_MEL_FREQS * spectogram_samples_per_slice))

	for i in range(n_slices):
		x[i, :] = mixed_mel_spectogram[:, (i * spectogram_samples_per_slice):((i + 1) * spectogram_samples_per_slice)].flatten()
		y[i, :] = signal1_mel_spectogram[:, (i * spectogram_samples_per_slice):((i + 1) * spectogram_samples_per_slice)].flatten()

	return x, y, mixed_signal


def read_audio_data(source_dir_path1, source_dir_path2, max_pairs):
	print("reading dataset...")

	source_file_paths1 = [os.path.join(source_dir_path1, f) for f in os.listdir(source_dir_path1)]
	source_file_paths2 = [os.path.join(source_dir_path2, f) for f in os.listdir(source_dir_path2)]

	x = []
	y = []

	n_pairs = min(len(source_file_paths1), len(source_file_paths2), max_pairs)
	for i in range(n_pairs):
		x_i, y_i, _ = prepare_sample(source_file_paths1[i], source_file_paths2[i])
		x.append(x_i)
		y.append(y_i)

	return np.concatenate(x), np.concatenate(y)


def reconstruct_signal(y, sample_rate):
	mel_converter = MelConverter(sample_rate)

	slice_mel_spectograms = [y[i, :].reshape((MelConverter.N_MEL_FREQS, -1)) for i in range(y.shape[0])]
	full_mel_spectogram = np.concatenate(slice_mel_spectograms, axis=1)

	return mel_converter.reconstruct_signal_from_mel_spectogram(full_mel_spectogram)


def train(args):
	x_train, y_train = read_audio_data(args.train_source_dir1, args.train_source_dir2, max_pairs=500)

	separator = AudioSourceSeparator()
	separator.init_model(spectogram_size=x_train.shape[1])
	separator.train(x_train, y_train)
	separator.dump(args.model_cache, args.weights_cache)


def evaluate(args):
	x_test, y_test = read_audio_data(args.test_source_dir1, args.test_source_dir2, max_pairs=10)

	separator = AudioSourceSeparator.load(args.model_cache, args.weights_cache)
	score = separator.evaluate(x_test, y_test)
	print("score: %.2f" % score)


def predict(args):
	separator = AudioSourceSeparator.load(args.model_cache, args.weights_cache)

	prediction_output_dir = os.path.join(args.prediction_output_dir, '{:%Y-%m-%d_%H-%M-%S}'.format(datetime.now()))
	os.mkdir(prediction_output_dir)

	source_file_paths1 = [os.path.join(args.test_source_dir1, f) for f in os.listdir(args.test_source_dir1)]
	source_file_paths2 = [os.path.join(args.test_source_dir2, f) for f in os.listdir(args.test_source_dir2)]

	n_pairs = min(len(source_file_paths1), len(source_file_paths2))

	for i in range(n_pairs):
		x, _, mixed_signal = prepare_sample(source_file_paths1[i], source_file_paths2[i])
		y_predicted = separator.predict(x)

		reconstructed_signal = reconstruct_signal(y_predicted, mixed_signal.get_sample_rate())

		source_name1 = os.path.splitext(os.path.basename(source_file_paths1[i]))[0]
		source_name2 = os.path.splitext(os.path.basename(source_file_paths2[i]))[0]

		source_prediction_dir_path = os.path.join(prediction_output_dir, source_name1 + "_" + source_name2)
		os.mkdir(source_prediction_dir_path)

		reconstructed_signal.save_to_wav_file(os.path.join(source_prediction_dir_path, "predicted.wav"))
		mixed_signal.save_to_wav_file(os.path.join(source_prediction_dir_path, "mix.wav"))

		shutil.copy(source_file_paths1[i], source_prediction_dir_path)
		shutil.copy(source_file_paths2[i], source_prediction_dir_path)


def main():
	parser = argparse.ArgumentParser(add_help=False)
	action_parsers = parser.add_subparsers()

	train_parser = action_parsers.add_parser("train")
	train_parser.add_argument("train_source_dir1", type=str)
	train_parser.add_argument("train_source_dir2", type=str)
	train_parser.add_argument("model_cache", type=str)
	train_parser.add_argument("weights_cache", type=str)
	train_parser.set_defaults(func=train)

	predict_parser = action_parsers.add_parser("predict")
	predict_parser.add_argument("test_source_dir1", type=str)
	predict_parser.add_argument("test_source_dir2", type=str)
	predict_parser.add_argument("model_cache", type=str)
	predict_parser.add_argument("weights_cache", type=str)
	predict_parser.add_argument("prediction_output_dir", type=str)
	predict_parser.set_defaults(func=predict)

	args = parser.parse_args()
	args.func(args)

if __name__ == "__main__":
	main()
