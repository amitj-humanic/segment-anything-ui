from __future__ import annotations

import dataclasses
from typing import Callable

import cv2
import matplotlib.pyplot as plt
import numpy as np
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QLineEdit
from segment_anything import SamPredictor, automatic_mask_generator
from segment_anything.build_sam import Sam
from skimage.measure import regionprops
from sympy import N
import torch


def get_cmap(n, name='hsv'):
    '''Returns a function that maps each index in 0, 1, ..., n-1 to a distinct
    RGB color; the keyword argument name must be a standard mpl colormap name.'''
    return plt.cm.get_cmap(name, n)


@dataclasses.dataclass()
class AutomaticMaskGeneratorSettings:
    points_per_side: int = 32
    pred_iou_thresh: float = 0.88
    stability_score_thresh: float = 0.95
    stability_score_offset: float = 1.0
    box_nms_thresh: float = 0.7
    crop_n_layers: int = 0
    crop_nms_thresh: float = 0.7


class LabelValueParam(QWidget):
    def __init__(self, label_text, default_value, value_type_converter: Callable = lambda x: x, parent=None):
        super().__init__(parent)
        self.layout = QVBoxLayout(self)
        self.label = QLabel(self, text=label_text)
        self.value = QLineEdit(self, text=default_value)
        self.layout.addWidget(self.label)
        self.layout.addWidget(self.value)
        self.converter = value_type_converter

    def get_value(self):
        return self.converter(self.value.text())


class CustomForm(QWidget):

    def __init__(self, parent: QWidget, automatic_mask_generator_settings: AutomaticMaskGeneratorSettings) -> None:
        super().__init__(parent)
        self.layout = QVBoxLayout(self)
        self.widgets = []

        for field in dataclasses.fields(automatic_mask_generator_settings):
            widget = LabelValueParam(field.name, str(field.default), field.type)
            self.widgets.append(widget)
            self.layout.addWidget(widget)

    def get_values(self):
        return AutomaticMaskGeneratorSettings(**{widget.label.text(): widget.get_value() for widget in self.widgets})


class MasksAnnotation:
    DEFAULT_LABEL = "default"

    def __init__(self) -> None:
        self.masks = []
        self.xymasks = []
        self.label_map = {}
        self.mask_id: int = -1

    def add_mask(self, mask, label: str | None = None):
        self.masks.append(mask)
        self.label_map[len(self.masks)] = self.DEFAULT_LABEL if label is None else label

    def add_label(self, mask_id: int, label: str):
        self.label_map[mask_id] = label

    def get_mask(self, mask_id: int):
        #return self.masks[mask_id]
        return self.xymasks[mask_id]

    def get_label(self, mask_id: int):
        return self.label_map[mask_id]

    def get_current_mask(self):
        return self.masks[self.mask_id]

    def set_current_mask(self, mask, label: str = None):
        self.masks[self.mask_id] = mask
        self.label_map[self.mask_id] = self.DEFAULT_LABEL if label is None else label

    def __getitem__(self, mask_id: int):
        return self.get_mask(mask_id)

    def __setitem__(self, mask_id: int, value):
        self.masks[mask_id] = value

    def __len__(self):
        return len(self.masks)

    def __iter__(self):
        return iter(zip(self.masks, self.label_map.values()))

    def __next__(self):
        if self.mask_id >= len(self.masks):
            raise StopIteration
        return self.masks[self.mask_id]

    def add_xymask(self, mask):
        self.xymasks.append(mask)

    def append(self, mask, label: str | None = None):
        self.add_mask(mask, label)

    def pop(self, mask_id: int = -1):
        mask = self.masks.pop(mask_id)
        self.label_map.pop(mask_id + 1)
        new_label_map = {}
        for index, value in enumerate(self.label_map.values()):
            new_label_map[index + 1] = value
        self.label_map = new_label_map
        return mask

    @classmethod
    def from_masks(cls, masks, labels: list[str] | None = None):
        annotation = cls()
        if labels is None:
            labels = [None] * len(masks)
        for mask, label in zip(masks, labels):
            annotation.append(mask, label)
        return annotation


@dataclasses.dataclass()
class Annotator:
    sam: Sam | None = None
    embedding: torch.Tensor | None = None
    image: np.ndarray | None = None
    masks: MasksAnnotation = dataclasses.field(default_factory=MasksAnnotation)
    predictor: SamPredictor | None = None
    visualization: np.ndarray | None = None
    last_mask: np.ndarray | None = None
    partial_mask: np.ndarray | None = None
    merged_mask: np.ndarray | None = None
    parent: QWidget | None = None
    cmap: plt.cm = None

    def __post_init__(self):
        self.MAX_MASKS = 10
        self.cmap = get_cmap(self.MAX_MASKS)

    def set_image(self, image: np.ndarray):
        self.image = image
        return self

    def make_embedding(self):
        if self.sam is None:
            return
        self.predictor = SamPredictor(self.sam)
        self.predictor.set_image(self.image)

    def predict_all(self, settings: AutomaticMaskGeneratorSettings):
        generator = automatic_mask_generator.SamAutomaticMaskGenerator(
            model=self.sam,
            **dataclasses.asdict(settings)
        )
        masks = generator.generate(self.image)
        print(f'--> {__file__} predict_all {masks = }')
        masks = [(m["segmentation"] * 255).astype(np.uint8) for m in masks]
        label = self.parent.annotation_layout.label_picker.currentItem().text()
        print(f'predict_all {label = }')
        self.masks = MasksAnnotation.from_masks(masks, [label, ] * len(masks))
        self.cmap = get_cmap(len(self.masks))

    def make_prediction(self, annotation: dict):
        print(f'--> {__file__} make_prediction {annotation["bounding_boxes"] = }')
        masks, scores, logits = self.predictor.predict(
            point_coords=annotation["points"],
            point_labels=annotation["labels"],
            box=annotation["bounding_boxes"],
            multimask_output=False
        )
        mask = masks[0]
        print(f'--> {__file__} make_prediction1 {self.last_mask = }')
        if self.last_mask:
            print(f'{self.last_mask.sum()}')
        self.last_mask = mask * 255
        print(f'--> {__file__} make_prediction255 {self.last_mask.sum()} {self.last_mask = }')

        self.masks.add_xymask(self.last_mask)

    def pick_partial_mask(self):
        if self.partial_mask is None:
            self.partial_mask = self.last_mask.copy()
        else:
            self.partial_mask = np.maximum(self.last_mask, self.partial_mask)
        self.last_mask = None

    def move_current_mask_to_background(self):
        self.masks.set_current_mask(self.masks.get_current_mask() * 0.5)

    def merge_masks(self):
        new_mask = np.bitwise_or(self.last_mask, self.merged_mask)
        self.masks.set_current_mask(new_mask, self.parent.annotation_layout.label_picker.currentItem().text())
        self.merged_mask = None

    def visualize_last_mask(self, label: str | None = None):
        last_mask = np.zeros_like(self.image)
        last_mask[:, :, 1] = self.last_mask
        if self.partial_mask is not None:
            last_mask[:, :, 0] = self.partial_mask
        if self.merged_mask is not None:
            last_mask[:, :, 2] = self.merged_mask
        if label is not None:
            props = regionprops(self.last_mask)[0]
            cv2.putText(
                last_mask,
                label,
                (int(props.centroid[1]), int(props.centroid[0])),
                cv2.FONT_HERSHEY_SIMPLEX,
                1.0,
                [255, 255, 255],
                2
            )
        self.parent.update(cv2.addWeighted(self.image.copy() if self.visualization is None else self.visualization.copy(), 0.8, last_mask, 0.5, 0))

    def visualize_mask(self) -> tuple:
        mask_argmax = self.make_instance_mask()
        visualization = np.zeros_like(self.image)
        border = np.zeros(self.image.shape[:2], dtype=np.uint8)
        for i in range(1, np.amax(mask_argmax) + 1):
            color = self.cmap(i)
            single_mask = np.zeros_like(mask_argmax)
            single_mask[mask_argmax == i] = 1
            visualization[mask_argmax == i, :] = np.array(color[:3]) * 255
            border += single_mask - cv2.erode(
                single_mask, np.ones((3, 3), np.uint8), iterations=1)
            label = self.masks.get_label(i)
            single_mask_center = np.mean(np.where(single_mask == 1), axis=1)
            if self.parent.settings.is_show_text():
                cv2.putText(
                    visualization,
                    label,
                    (int(single_mask_center[1]), int(single_mask_center[0])),
                    cv2.FONT_HERSHEY_PLAIN,
                    0.5,
                    [255, 255, 255],
                    1
                )
        border = (border == 0).astype(np.uint8)
        return visualization, border

    def make_instance_mask(self, mask_path = '', actual_shape = (32,32)):
        #print(f'--> make_instance_mask {mask_path = } {len(self.masks.xymasks) = }')
        #if mask_path is not None and mask_path != '' and len(self.masks.xymasks):
        #    print(f'--> save mask {mask_path = } {actual_shape = }') 
        #    mask_img = cv2.resize(self.masks.xymasks[0], actual_shape, interpolation=cv2.INTER_NEAREST)
        #    cv2.imwrite(f'{mask_path}', mask_img)

        background = np.zeros_like(self.masks[0]) + 1
        #mask_argmax = np.argmax(np.concatenate([np.expand_dims(background, 0), np.array(self.masks.masks)], axis=0), axis=0).astype(np.uint8)
        mask_argmax = np.argmax(np.concatenate([np.expand_dims(background, 0), np.array(self.masks.xymasks)], axis=0), axis=0).astype(np.uint8)
        return mask_argmax

    def merge_image_visualization(self):
        if not len(self.masks):
            return self.image
        visualization, border = self.visualize_mask()
        self.visualization = cv2.addWeighted(self.image, 0.8, visualization, 0.7, 0) * border[:, :, np.newaxis]
        return self.visualization

    def remove_last_mask(self):
        self.masks.pop()

    def make_labels(self):
        return self.masks.label_map

    def save_mask(self, label: str = MasksAnnotation.DEFAULT_LABEL, mask_path = '', actual_shape = (32,32)):
        if self.partial_mask is not None:
            last_mask = self.partial_mask
            self.partial_mask = None
        else:
            last_mask = self.last_mask

        print(f'--> make_instance_mask {mask_path = } {len(self.masks.xymasks) = }')
        if mask_path is not None and mask_path != '' and last_mask is not None:
            print(f'--> save mask {mask_path = } {actual_shape = }') 
            mask_img = cv2.resize(last_mask, actual_shape, interpolation=cv2.INTER_NEAREST)
            cv2.imwrite(f'{mask_path}', mask_img)

        self.masks.append(last_mask, label=label)
        if len(self.masks) >= self.MAX_MASKS:
            self.MAX_MASKS += 10
            self.cmap = get_cmap(self.MAX_MASKS)

    def clear(self):
        self.last_mask = None
        self.visualization = None
        self.masks = MasksAnnotation()
        self.partial_mask = None
