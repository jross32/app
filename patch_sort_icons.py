from pathlib import Path

path = Path("templates/profile_detail.html")
text = path.read_text()
old = """.table-header th.sortable::after {
    content: '\\f0dc';
    font-family: "Font Awesome 6 Free";
    font-weight: 900;
    margin-left: 6px;
    opacity: 0.45;
    font-size: 0.75rem;
  }
  .table-header th.sorted-asc::after { content: '\\f062'; opacity: 0.9; }
  .table-header th.sorted-desc::after { content: '\\f063'; opacity: 0.9; }
"""
new = """.table-header th.sortable::after {
    content: '⇅';
    font-weight: 700;
    margin-left: 6px;
    opacity: 0.45;
    font-size: 0.75rem;
  }
  .table-header th.sorted-asc::after { content: '▲'; opacity: 0.9; }
  .table-header th.sorted-desc::after { content: '▼'; opacity: 0.9; }
"""
if old not in text:
    raise SystemExit("old block not found")
path.write_text(text.replace(old, new), encoding="utf-8")
print("sort icons replaced")
