"""公开基线检查；功能边界由容器外部验收器复核。"""

import unittest

import slugify


class SmokeTest(unittest.TestCase):
    def test_import(self):
        self.assertTrue(callable(slugify.slugify))


if __name__ == "__main__":
    unittest.main()
