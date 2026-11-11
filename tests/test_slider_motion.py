"""Slider motion must stay in bounds and finish close to the detected gap."""

import importlib
import importlib.util
import math
import random
import unittest


class SliderMotionTests(unittest.TestCase):
    def motion(self, distance=120, maximum=242, seed=1):
        self.assertIsNotNone(importlib.util.find_spec('slider_motion'), 'Add a bounded variable-speed motion generator')
        return importlib.import_module('slider_motion').make_slider_motion(
            distance, maximum, rng=random.Random(seed),
        )

    def test_moves_past_the_gap_and_returns_to_an_accurate_endpoint(self):
        points = self.motion()
        self.assertGreater(max(p.x for p in points), 121)
        self.assertTrue(any(b.x < a.x for a, b in zip(points, points[1:])))
        self.assertLessEqual(abs(points[-1].x - 120), .25)
        self.assertEqual(points[-1].y, 0)

    def test_speed_and_timing_vary_without_excessive_delay(self):
        points = self.motion()
        intervals = [b.time_ms-a.time_ms for a, b in zip(points, points[1:])]
        self.assertTrue(all(dt > 0 for dt in intervals))
        self.assertGreater(max(intervals)-min(intervals), 3)
        velocities = [(b.x-a.x)/(b.time_ms-a.time_ms) for a, b in zip(points, points[1:])]
        self.assertGreater(max(velocities), .15)
        self.assertTrue(any(v < 0 for v in velocities))
        self.assertTrue(any(abs(p.y) > .2 for p in points))
        self.assertGreaterEqual(points[-1].time_ms, 1300)
        self.assertLessEqual(points[-1].time_ms, 2000)

    def test_near_the_track_end_it_never_moves_beyond_the_limit(self):
        for distance in (1, 100, 241.9, 242):
            for seed in range(12):
                with self.subTest(distance=distance, seed=seed):
                    points = self.motion(distance=distance, seed=seed)
                    self.assertTrue(all(0 <= p.x <= 242 and abs(p.y) <= 2 for p in points))
                    self.assertLessEqual(abs(points[-1].x-distance), .25)

    def test_successive_attempts_do_not_repeat_the_same_motion(self):
        self.assertNotEqual(self.motion(seed=1), self.motion(seed=2))

    def test_vertical_motion_survives_browser_integer_coordinates(self):
        for seed in range(200):
            with self.subTest(seed=seed):
                points = self.motion(seed=seed)
                recorded_y = {23} | {math.floor(246 + point.y) - 223 for point in points}
                self.assertGreater(len(recorded_y), 1)

    def test_vertical_motion_stays_within_two_pixels_including_noise(self):
        for seed in (112, 7247):
            with self.subTest(seed=seed):
                self.assertTrue(all(abs(point.y) <= 2 for point in self.motion(seed=seed)))

    def test_invalid_geometry_is_rejected(self):
        self.assertIsNotNone(importlib.util.find_spec('slider_motion'))
        generator = importlib.import_module('slider_motion').make_slider_motion
        for distance, maximum in ((0, 242), (-1, 242), (243, 242), (100, 0), (float('nan'), 242), (100, float('inf'))):
            with self.subTest(distance=distance, maximum=maximum):
                with self.assertRaises(ValueError):
                    generator(distance, maximum)


if __name__ == '__main__':
    unittest.main()
