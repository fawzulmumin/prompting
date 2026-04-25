# utils/markdown_to_html_test.py

import time
from app.utils.markdown_to_html import markdown_to_safe_html

def print_test(name, inp, expected=None):
    print(f"\n==== {name} ====")
    out = markdown_to_safe_html(inp)
    print(out[:300])
    if expected:
        for e in expected:
            print("✓" if e in out else "✗", e)

def run_all_tests():
    print_test("Basic", "# Hello\n**Bold**", ["<h1>","<strong>"])
    print_test("Link", "[G](https://google.com)", ["noopener noreferrer"])
    print_test("XSS", "<script>alert(1)</script>", [])
    print_test("Image", "![a](https://x.com/a.jpg)", ["<img"])
    print_test("Code", "```python\nprint(1)\n```", ["<pre>"])
    
    # Performance
    md = "# Test\n\n" + ("Hello world " * 50 + "\n\n") * 10
    start = time.time()
    for _ in range(100):
        markdown_to_safe_html(md)
    print(f"\nPerf: {time.time()-start:.3f}s")

if __name__ == "__main__":
    run_all_tests()
