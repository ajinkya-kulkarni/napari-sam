from qtpy.QtWidgets import QVBoxLayout, QPushButton, QWidget, QLabel, QComboBox, QRadioButton
from qtpy import QtCore
import napari
import numpy as np
from enum import Enum
from collections import deque, defaultdict
import inspect
from segment_anything import SamPredictor, sam_model_registry
from segment_anything.automatic_mask_generator import SamAutomaticMaskGenerator
from napari_sam.utils import get_weights_path, get_cached_weight_types
import torch
from vispy.util.keys import CONTROL
import copy


class AnnotatorMode(Enum):
    NONE = 0
    CLICK = 1
    BBOX = 2
    AUTO = 3


class SamWidget(QWidget):
    def __init__(self, napari_viewer):
        super().__init__()
        self.viewer = napari_viewer

        self.annotator_mode = AnnotatorMode.NONE

        self.device = "cuda" if torch.cuda.is_available() else "cpu"

        self.setLayout(QVBoxLayout())
        self.layer_types = {"image": napari.layers.image.image.Image, "labels": napari.layers.labels.labels.Labels}

        l_model_type = QLabel("Select model type:")
        self.layout().addWidget(l_model_type)

        self.cb_model_type = QComboBox()
        self.layout().addWidget(self.cb_model_type)

        self.btn_load_model = QPushButton("Load model")
        self.btn_load_model.clicked.connect(self._load_model)
        self.layout().addWidget(self.btn_load_model)
        self.is_model_loaded = False
        self.init_model_type_combobox()

        l_image_layer = QLabel("Select input image layer:")
        self.layout().addWidget(l_image_layer)

        self.cb_image_layers = QComboBox()
        self.cb_image_layers.addItems(self.get_layer_names("image"))
        self.layout().addWidget(self.cb_image_layers)

        l_label_layer = QLabel("Select output labels layer:")
        self.layout().addWidget(l_label_layer)

        self.cb_label_layers = QComboBox()
        self.cb_label_layers.addItems(self.get_layer_names("labels"))
        self.layout().addWidget(self.cb_label_layers)

        self.comboboxes = [{"combobox": self.cb_image_layers, "layer_type": "image"}, {"combobox": self.cb_label_layers, "layer_type": "labels"}]

        self.rb_click = QRadioButton("Click")
        self.rb_click.setChecked(True)
        self.layout().addWidget(self.rb_click)

        self.rb_bbox = QRadioButton("Bounding Box (WIP)")
        self.rb_bbox.setEnabled(False)
        self.rb_bbox.setStyleSheet("color: gray")
        self.layout().addWidget(self.rb_bbox)

        self.rb_auto = QRadioButton("Everything")
        # self.rb_auto.setEnabled(False)
        # self.rb_auto.setStyleSheet("color: gray")
        self.layout().addWidget(self.rb_auto)

        self.btn_activate = QPushButton("Activate")
        self.btn_activate.clicked.connect(self._activate)
        self.btn_activate.setEnabled(False)
        self.is_active = False
        self.layout().addWidget(self.btn_activate)

        self.l_info = QLabel("Info: \n \n"
                             "Positive Click: Middle Mouse Button\n \n"
                             "Negative Click: Control + Middle Mouse Button \n \n"
                             "Undo: Control + Z")
        # self.l_info_positive = QLabel("Middle Mouse Button: Positive Click")
        # self.l_info_negative = QLabel("Control + Middle Mouse Button: Negative Click")
        # self.l_info_undo = QLabel("Undo: Control + Z")
        self.layout().addWidget(self.l_info)
        # self.layout().addWidget(self.l_info_positive)
        # self.layout().addWidget(self.l_info_negative)
        # self.layout().addWidget(self.l_info_undo)

        self.image_name = None
        self.image_layer = None
        self.label_layer = None
        self.label_layer_changes = None
        self.points_layer = None

        self.init_comboboxes()

        self.sam_model = None
        self.sam_predictor = None
        self.sam_logits = None

        self.points = defaultdict(list)
        self.point_label = None

        self.viewer.window.qt_viewer.layers.model().filterAcceptsRow = self._myfilter

    def init_model_type_combobox(self):
        model_types = list(sam_model_registry.keys())
        cached_weight_types = get_cached_weight_types(model_types)
        entries = []
        for name, is_cached in cached_weight_types.items():
            if is_cached:
                entries.append("{} (Cached)".format(name))
            else:
                entries.append("{} (Auto-Download)".format(name))
        self.cb_model_type.addItems(entries)

        if cached_weight_types[list(cached_weight_types.keys())[self.cb_model_type.currentIndex()]]:
            self.btn_load_model.setText("Load model")
        else:
            self.btn_load_model.setText("Download and load model")

        self.cb_model_type.currentTextChanged.connect(self.on_model_type_combobox_change)

    def on_model_type_combobox_change(self):
        model_types = list(sam_model_registry.keys())
        cached_weight_types = get_cached_weight_types(model_types)

        if cached_weight_types[list(cached_weight_types.keys())[self.cb_model_type.currentIndex()]]:
            self.btn_load_model.setText("Load model")
        else:
            self.btn_load_model.setText("Download and load model")

    def init_comboboxes(self):
        for combobox_dict in self.comboboxes:
            # If current active layer is of the same type of layer that the combobox accepts then set it as selected layer in the combobox.
            active_layer = self.viewer.layers.selection.active
            if combobox_dict["layer_type"] == "all" or isinstance(active_layer, self.layer_types[combobox_dict["layer_type"]]):
                index = combobox_dict["combobox"].findText(active_layer.name, QtCore.Qt.MatchFixedString)
                if index >= 0:
                    combobox_dict["combobox"].setCurrentIndex(index)

        # Inform all comboboxes on layer changes with the viewer.layer_change event
        self.viewer.events.layers_change.connect(self._on_layers_changed)

        # viewer.layer_change event does not inform about layer name changes, so we have to register a separate event to each layer and each layer that will be created

        # Register an event to all existing layers
        for layer_name in self.get_layer_names():
            layer = self.viewer.layers[layer_name]

            @layer.events.name.connect
            def _on_rename(name_event):
                self._on_layers_changed()

        # Register an event to all layers that will be created
        @self.viewer.layers.events.inserted.connect
        def _on_insert(event):
            layer = event.value

            @layer.events.name.connect
            def _on_rename(name_event):
                self._on_layers_changed()

        self._init_comboboxes_callback()

    def _on_layers_changed(self):
        for combobox_dict in self.comboboxes:
            layer = combobox_dict["combobox"].currentText()
            layers = self.get_layer_names(combobox_dict["layer_type"])
            combobox_dict["combobox"].clear()
            combobox_dict["combobox"].addItems(layers)
            index = combobox_dict["combobox"].findText(layer, QtCore.Qt.MatchFixedString)
            if index >= 0:
                combobox_dict["combobox"].setCurrentIndex(index)
        self._on_layers_changed_callback()

    def get_layer_names(self, type="all", exclude_hidden=True):
        layers = self.viewer.layers
        filtered_layers = []
        for layer in layers:
            if (type == "all" or isinstance(layer, self.layer_types[type])) and ((not exclude_hidden) or (exclude_hidden and "<hidden>" not in layer.name)):
                filtered_layers.append(layer.name)
        return filtered_layers

    def _init_comboboxes_callback(self):
        self._check_activate_btn()

    def _on_layers_changed_callback(self):
        self._check_activate_btn()

    def _check_activate_btn(self):
        if self.cb_image_layers.currentText() != "" and self.cb_label_layers.currentText() != "" and self.is_model_loaded:
            self.btn_activate.setEnabled(True)
        else:
            self.btn_activate.setEnabled(False)
            self._deactivate()

    def _load_model(self):
        model_types = list(sam_model_registry.keys())
        model_type = model_types[self.cb_model_type.currentIndex()]
        self.sam_model = sam_model_registry[model_type](
            get_weights_path(model_type)
        )
        self.sam_model.to(self.device)
        self.sam_predictor = SamPredictor(self.sam_model)
        self.sam_anything_predictor = SamAutomaticMaskGenerator(self.sam_model)
        self.is_model_loaded = True
        self._check_activate_btn()

    def _activate(self):
        if not self.is_active and self.rb_click.isChecked():
            self.is_active = True
            self.btn_activate.setText("Deactivate")
            self.rb_bbox.setEnabled(False)
            self.rb_auto.setEnabled(False)
            self.rb_bbox.setStyleSheet("color: gray")
            self.rb_auto.setStyleSheet("color: gray")
            self.image_name = self.cb_image_layers.currentText()
            self.image_layer = self.viewer.layers[self.cb_image_layers.currentText()]
            self.label_layer = self.viewer.layers[self.cb_label_layers.currentText()]
            self.label_layer_changes = None
            self.label_layer.keymap = {}
            self.widget_callbacks = []
            self.annotator_mode = AnnotatorMode.CLICK

            self._history_limit = self.label_layer._history_limit
            self._reset_history()

            self.viewer.mouse_drag_callbacks.append(self.callback_click)

            self.set_image()
            self.update_points_layer(None)

            @self.label_layer.bind_key('Control-Z')
            def on_undo(layer):
                """Undo the last paint or fill action since the view slice has changed."""
                self.undo()
                layer.undo()

            @self.label_layer.bind_key('Control-Shift-Z')
            def on_redo(layer):
                """Redo any previously undone actions."""
                self.redo()
                layer.redo()

            # @self.viewer.bind_key('Control-RightClick')
            # def tmp(layer):
            #     print("sdsd")

        elif not self.is_active and self.rb_auto.isChecked():
            self.is_active = True
            self.btn_activate.setText("Deactivate")
            self.rb_bbox.setEnabled(False)
            self.rb_click.setEnabled(False)
            self.rb_bbox.setStyleSheet("color: gray")
            self.rb_click.setStyleSheet("color: gray")
            self.image_name = self.cb_image_layers.currentText()
            self.image_layer = self.viewer.layers[self.cb_image_layers.currentText()]
            self.label_layer = self.viewer.layers[self.cb_label_layers.currentText()]
            self.label_layer_changes = None
            self.annotator_mode = AnnotatorMode.AUTO

            if self.image_layer.ndim != 2:
                raise RuntimeError("Only 2D images are supported at the moment.")
            image = self.image_layer.data
            if not self.image_layer.rgb:
                image = np.stack((image,)*3, axis=-1)  # Expand to 3-channel image
            image = image[..., :3]  # Remove a potential alpha channel
            records = self.sam_anything_predictor.generate(image)
            masks = np.asarray([record["segmentation"] for record in records])
            prediction = np.argmax(masks, axis=0)
            self.label_layer.data = prediction
        else:
            self._deactivate()

    def _deactivate(self):
        self.is_active = False
        self.btn_activate.setText("Activate")
        self.remove_all_widget_callbacks()
        if self.label_layer is not None:
            self.label_layer.keymap = {}
        if self.points_layer is not None:
            self.viewer.layers.remove(self.points_layer)
        self.image_name = None
        self.image_layer = None
        self.label_layer = None
        self.label_layer_changes = None
        self.points_layer = None
        self.annotator_mode = AnnotatorMode.NONE
        self.points = defaultdict(list)
        self.point_label = None
        self.sam_logits = None
        self.rb_click.setEnabled(True)
        self.rb_auto.setEnabled(True)
        self.rb_click.setStyleSheet("color: black")
        self.rb_auto.setStyleSheet("color: black")
        self._reset_history()

    def callback_click(self, layer, event):
        if self.annotator_mode == AnnotatorMode.CLICK:
            data_coordinates = self.image_layer.world_to_data(event.position)
            coords = np.round(data_coordinates).astype(int)
            if (not CONTROL in event.modifiers) and event.button == 3:  # Positive middle click
                self.do_click(coords, 1)
                yield
            elif CONTROL in event.modifiers and event.button == 3:  # Negative middle click
                self.do_click(coords, 0)
                yield

    def set_image(self):
        if self.image_layer is not None:
            if self.image_layer.ndim != 2:
                raise RuntimeError("Only 2D images are supported at the moment.")
            image = self.image_layer.data
            if not self.image_layer.rgb:
                image = np.stack((image,)*3, axis=-1)  # Expand to 3-channel image
            image = image[..., :3]  # Remove a potential alpha channel
            self.sam_predictor.set_image(image)

    def do_click(self, coords, is_positive):
        self._save_history({"points": copy.deepcopy(self.points), "logits": self.sam_logits, "point_label": self.point_label})

        self.point_label = self.label_layer.selected_label
        if not is_positive:
            self.point_label = 0

        self.points[self.point_label].append(coords)

        self.run(self.points, self.point_label)
        self.label_layer._save_history((self.label_layer_changes["indices"], self.label_layer_changes["old_values"], self.label_layer_changes["new_values"]))

    def run(self, points, point_label):
        self.update_points_layer(points)

        if points:
            points_flattened = []
            labels_flattended = []
            for label, label_points in points.items():
                points_flattened.extend(label_points)
                label = int(label == point_label)
                labels = [label] * len(label_points)
                labels_flattended.extend(labels)

            points_flattened = np.flip(points_flattened, axis=-1)

            prediction, _, self.sam_logits = self.sam_predictor.predict(
                point_coords=points_flattened,
                point_labels=np.asarray(labels_flattended),
                mask_input=self.sam_logits,
                multimask_output=False,
            )
            prediction = prediction[0]
        else:
            prediction = np.zeros_like(self.label_layer.data)

        changed_indices = np.where(prediction == 1)
        index_labels_old = self.label_layer.data[changed_indices]
        self.label_layer.data[prediction] = point_label
        index_labels_new = self.label_layer.data[changed_indices]
        self.label_layer_changes = {"indices": changed_indices, "old_values": index_labels_old, "new_values": index_labels_new}
        self.label_layer.data = self.label_layer.data
        self.label_layer.refresh()

    def update_points_layer(self, points):
        selected_layer = self.viewer.layers.selection.active
        if self.points_layer is not None:
            self.viewer.layers.remove(self.points_layer)

        points_flattened = []
        colors_flattended = []
        if points is not None:
            for label, label_points in points.items():
                points_flattened.extend(label_points)
                color = self.label_layer.get_color(label)
                colors = [color] * len(label_points)
                colors_flattended.extend(colors)

        self.points_layer = self.viewer.add_points(name="Ignore this layer <hidden>", data=np.asarray(points_flattened), face_color=colors_flattended)

        self.viewer.layers.selection.active = selected_layer

    def remove_all_widget_callbacks(self):
        callback_types = ['mouse_double_click_callbacks', 'mouse_drag_callbacks', 'mouse_move_callbacks',
                          'mouse_wheel_callbacks']
        for callback_type in callback_types:
            callback_list = getattr(self.viewer, callback_type)
            for callback in callback_list:
                if inspect.ismethod(callback) and callback.__self__ == self:
                    callback_list.remove(callback)

    def _reset_history(self, event=None):
        self._undo_history = deque()
        self._redo_history = deque()

    def _save_history(self, history_item):
        """Save a history "atom" to the undo history.

        A history "atom" is a single change operation to the array. A history
        *item* is a collection of atoms that were applied together to make a
        single change. For example, when dragging and painting, at each mouse
        callback we create a history "atom", but we save all those atoms in
        a single history item, since we would want to undo one drag in one
        undo operation.

        Parameters
        ----------
        history_item : 2-tuple of region prop dicts
        """
        self._redo_history = deque()
        # if not self._block_saving:
        #     self._undo_history.append([value])
        # else:
        #     self._undo_history[-1].append(value)
        self._undo_history.append(history_item)

    def _load_history(self, before, after, undoing=True):
        """Load a history item and apply it to the array.

        Parameters
        ----------
        before : list of history items
            The list of elements from which we want to load.
        after : list of history items
            The list of element to which to append the loaded element. In the
            case of an undo operation, this is the redo queue, and vice versa.
        undoing : bool
            Whether we are undoing (default) or redoing. In the case of
            redoing, we apply the "after change" element of a history element
            (the third element of the history "atom").

        See Also
        --------
        Labels._save_history
        """
        if len(before) == 0:
            return

        history_item = before.pop()
        after.append(history_item)

        self.points = history_item["points"]
        self.point_label = history_item["point_label"]
        self.sam_logits = history_item["logits"]
        # self.run(history_item["points"], history_item["point_label"])
        self.update_points_layer(self.points)

    def undo(self):
        self._load_history(
            self._undo_history, self._redo_history, undoing=True
        )

    def redo(self):
        self._load_history(
            self._redo_history, self._undo_history, undoing=False
        )
        raise RuntimeError("Redo currently not supported.")

    def _myfilter(self, row, parent):
        return "<hidden>" not in self.viewer.layers[row].name