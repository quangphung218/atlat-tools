import io
import sys
import tempfile
import unicodedata
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from atlat_tools import vn_style  # noqa: E402

SAMPLE = """# Báo cáo

Em sẽ tiến hành kiểm tra, có lẽ hơi lâu .

Bạn đọc file `hòa bình.md` , thủy triều lên , Kết Quả Kiểm Tra Cuối Cùng ổn, Hà Nội đẹp.

```
bạn trong code: ok
```

Hy vọng giúp ích cho anh.
"""


class VnStyleTest(unittest.TestCase):
    def setUp(self):
        self.l = vn_style.Linter()

    def codes(self, text):
        return [(line, code) for line, _, code, _ in self.l.lint(text)]

    def test_findings(self):
        c = self.codes(SAMPLE)
        self.assertIn((3, "first"), c)
        self.assertIn((3, "opener"), c)
        self.assertIn((3, "filler"), c)
        self.assertIn((3, "hedge"), c)
        self.assertIn((3, "vague"), c)
        self.assertIn((3, "space"), c)
        self.assertIn((5, "pronoun"), c)
        self.assertIn((5, "old"), c)
        self.assertIn((5, "title"), c)
        self.assertIn((11, "closer"), c)
        self.assertFalse(any(line == 8 for line, _ in c))  # code block skipped
        self.assertFalse(any(code == "title" and "Hà Nội" in msg for _, _, code, msg in self.l.lint(SAMPLE)))

    def test_inline_code_ignored_by_lint(self):
        self.assertEqual(self.codes("Mở file `hòa.md` nhé."), [])

    def test_modern_marks(self):
        self.assertEqual(vn_style.modern_marks("hòa thủy khỏe Hòa lòe"), "hoà thuỷ khoẻ Hoà loè")
        self.assertEqual(vn_style.modern_marks("hoàn thuyền quá mùa hoặc"), "hoàn thuyền quá mùa hoặc")  # closed syllables and non oa/oe/uy untouched
        self.assertEqual(vn_style.modern_marks("hoà"), "hoà")

    def test_fix_is_mechanical_only(self):
        fixed = self.l.fix(SAMPLE)
        self.assertIn("hơi lâu.", fixed)
        self.assertIn("`hòa bình.md`, thuỷ triều lên,", fixed)  # inline code untouched, prose modernised
        self.assertIn("bạn trong code: ok", fixed)
        self.assertIn("Bạn đọc", fixed)  # wording is never changed
        self.assertEqual(unicodedata.normalize("NFC", fixed), fixed)

    def test_nfc_warning(self):
        nfd = unicodedata.normalize("NFD", "Kết luận: xong.")
        self.assertIn("nfc", [c for _, _, c, _ in self.l.lint(nfd)])

    def test_custom_rules(self):
        l = vn_style.Linter({"pronouns": ["tớ"], "title_case": False, "new_marks": False})
        c = [code for _, _, code, _ in l.lint("Tớ Đi Chơi Về, bạn ơi hòa.")]
        self.assertEqual(c, ["pronoun"])

    def test_cli_and_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "a.md"
            f.write_text("Kết luận: thủy .\n")
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(vn_style.main([str(f)]), 0)       # warnings only
                self.assertEqual(vn_style.main([str(f), "--strict"]), 1)
                self.assertEqual(vn_style.main([str(f), "--fix", "--lang", "vi"]), 0)
            self.assertEqual(f.read_text(), "Kết luận: thuỷ.\n")
            f.write_text("Chào bạn.\n")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(vn_style.main([str(f)]), 1)       # forbidden pronoun is an error


if __name__ == "__main__":
    unittest.main()
