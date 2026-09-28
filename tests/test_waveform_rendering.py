"""Waveform drawing stays aligned at fractional scales and requests real detail."""
import unittest

import numpy as np
from PySide6.QtCore import QEvent, QRect
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication

from sound_file_manager.player import WaveformView
from . import dispose, qt_app


class ScaledWaveform(WaveformView):
    ratio = 1.0

    def devicePixelRatioF(self):
        return self.ratio


class WaveformRenderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qt_app()

    def view(self, ratio=1.5):
        view = dispose(self, ScaledWaveform())
        view.ratio = ratio
        view.resize(201, 101)  # fractional physical dimensions too
        view.position = 2  # keep the playhead outside this comparison
        view.set_file(100000, 10)
        rng = np.random.default_rng(15)
        peaks = rng.uniform(0.1, 0.95, (2, 4096)).astype(np.float32)
        view.set_levels(np.stack([peaks, peaks * 0.6]), ['A', 'B'])
        return view

    def render(self, view, strips=False):
        size = view.size() * view.ratio
        image = QImage(size, QImage.Format.Format_ARGB32)
        image.setDevicePixelRatio(view.ratio)
        image.fill(0)
        painter = QPainter(image)
        if strips:
            for x in range(0, view.width(), 5):
                painter.setClipRect(QRect(x, 0, min(5, view.width() - x), view.height()))
                view._paint(painter)
        else:
            view._paint(painter)
        painter.end()
        return np.frombuffer(image.bits(), np.uint32).copy()

    def test_partial_repaints_match_full_image_at_all_scales(self):
        for scale in (1.0, 1.25, 1.5, 2.0):
            with self.subTest(scale=scale):
                view = self.view(scale)
                np.testing.assert_array_equal(self.render(view), self.render(view, strips=True))

    def test_cached_detail_is_refined_after_resize(self):
        view = self.view(1.0)
        view.set_range(0.1, 0.12)
        view.set_detail(view.view, np.zeros((2, 2, 201), np.float32))
        view._detail_request = (view.view, 201)
        view._visible_levels(401)
        self.assertTrue(view._detail_timer.isActive())

    def test_one_bucket_per_sample_does_not_request_more(self):
        view = self.view()
        view.set_file(100, 1)
        view.set_detail(view.view, np.zeros((2, 2, 100), np.float32))
        view._visible_levels(302)
        self.assertFalse(view._detail_timer.isActive())

    def test_monitor_scale_change_invalidates_raster(self):
        view = self.view(1.0)
        self.render(view)
        self.assertIsNotNone(view._pixmap)
        view.ratio = 1.5
        QApplication.sendEvent(view, QEvent(QEvent.Type.DevicePixelRatioChange))
        self.assertIsNone(view._pixmap)
        self.render(view)
        self.assertEqual(view._pixmap.devicePixelRatioF(), 1.5)
