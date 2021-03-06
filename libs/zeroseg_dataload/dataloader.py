from skimage import io, transform, color
import os
import cv2
import math
import numpy as np
import scipy.io as sio
import torch
from torch.utils import data
import torchvision
import libs.zeroseg_dataload.transform as tf
from PIL import Image

class dataloader(data.Dataset):
	def __init__(self, obj_npy_path='/home/liuyuting/Code/CaGNet_game/dataset/zeroseg/obj_id.npy',
				 train_npy_path='/home/liuyuting/Code/CaGNet_game/dataset/zeroseg/train_dist.npy',
                 root_path='/media/adminer/data/zhijiang_zeroseg/', mode='train', resize=384):

		self.obj_npy_path = obj_npy_path
		self.train_npy_path = train_npy_path

		self.obj_dict = np.load(self.obj_npy_path, allow_pickle=True).item()
		self.train_dict = np.load(self.train_npy_path, allow_pickle=True).item()
		self.trainflag = mode

		self.all_img_names = []
		for img_names in self.train_dict:
			self.all_img_names.append(img_names)

		if mode == 'train' or mode == 'val':
			self.data_img_path = os.path.join(root_path, 'train/image')
			self.data_label_path = os.path.join(root_path, 'train/seg_img')
			self.transform = tf.Compose([tf.MaskRandResizedCrop(resize, 0.9, 1.0), \
										 # tf.MaskHFlip(), \
										 # tf.MaskColourJitter(p=1.0), \
										 # tf.MaskNormalise((0.485, 0.456, 0.406), (0.229, 0.224, 0.225)), \
										 tf.MaskNormalise((0, 0, 0), (1, 1, 1)), \
										 tf.MaskToTensor()
										 ])
			self.val_img_names, self.train_img_names = self.all_img_names[:500], self.all_img_names[500:]
			if mode == 'train':
				self.img_names = self.train_img_names
			else:
				self.img_names = self.val_img_names

		else:
			self.data_img_path = os.path.join(root_path, 'test/image')
			self.data_img_names = os.listdir(self.data_img_path)
			self.transform = tf.Compose([tf.MaskRandResizedCrop(resize, 0.9, 1.0), \
										 tf.MaskNormalise((0, 0, 0), (1, 1, 1)), \
										 tf.MaskToTensor()
										 ])



	def __len__(self):
		if self.trainflag == 'train':
			return len(self.train_img_names)
		elif self.trainflag == 'val':
			return len(self.val_img_names)
		elif self.trainflag == 'test':
			return len(self.data_img_names)
		else:
			raise

	def __getitem__(self, i):
		if self.trainflag == 'train' or self.trainflag == 'val':
			return self.traindata_getitem(i)
		else:
			return self.testdata_getitem(i)


	def traindata_getitem(self, index):
		cur_img_name = self.img_names[index]
		img_path = os.path.join(self.data_img_path, cur_img_name)
		img = cv2.imread(img_path, 1)
		img = img[...,::-1].copy()

		cur_mask_name_list = self.train_dict[cur_img_name]
		num_obj = len(cur_mask_name_list)

		id = []
		obj_gt = []
		for i in range(num_obj):
			obj_id = [x for x in cur_mask_name_list[i]][0]
			mask_path = os.path.join(self.data_label_path, cur_mask_name_list[i][obj_id])
			mask = cv2.imread(mask_path, 0)
			mask[mask != 0] = 1
			id.append(obj_id)
			obj_gt.append(mask)

		ignore_mask = np.zeros_like(obj_gt[0], dtype=np.long)
		label = np.zeros_like(obj_gt[0], dtype=np.long)
		for i in range(len(obj_gt)):
			ignore_mask += obj_gt[i]
			label += id[i] * obj_gt[i]
		ignore_mask = ignore_mask > 1
		label[np.where(ignore_mask)] = 255

		label = cv2.resize(label, dsize=(img.shape[1], img.shape[0]), interpolation=cv2.INTER_NEAREST)

		img, label = Image.fromarray(img), Image.fromarray(label)

		if self.transform:
			img, label = self.transform(img, label)
		label = label.squeeze(0)
		# label[label == 0] = 255

		#Visualization
		# label_numpy = np.uint8((label.unsqueeze(2).repeat(1, 1, 3).cpu().numpy()))
		# label_PIL = Image.fromarray(label_numpy)
		# label_PIL.show()
		# img_numpy = np.uint8((img.permute(1, 2, 0).cpu().numpy()*255))
		# img_numpy[label_numpy != 255] = 255
		# img_PIL = Image.fromarray(img_numpy)
		# img_PIL.show()

		return img.float(), label.long()

	def testdata_getitem(self, i):
		cur_img_name = self.data_img_names[i]
		img_path = os.path.join(self.data_img_path, cur_img_name)
		img = cv2.imread(img_path, 1)
		label = np.uint8(np.ones((img.shape[0], img.shape[1])))
		img = Image.fromarray(img)
		label = Image.fromarray(label)
		if self.transform:
			img, label  = self.transform(img, label)
		return img.float()

if __name__ == '__main__':
	d = dataloader(train=True)
	img = d[1]
	print(img.shape)

