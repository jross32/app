from PyPDF2 import PdfReader
import os

input_path = "data/team-manual-english.pdf"
output_path = "data/apa_rulebook.txt"

reader = PdfReader(input_path)
text = "\n\n".join(page.extract_text() or "" for page in reader.pages)

os.makedirs(os.path.dirname(output_path), exist_ok=True)
with open(output_path, "w", encoding="utf-8") as f:
    f.write(text)

print(f"Saved clean text to: {output_path}")
