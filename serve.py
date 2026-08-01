"""
serve.py  —  put this in your project ROOT (next to index.html)
Run: python serve.py
Then open: http://localhost:8080
"""
import http.server, socketserver, os

os.chdir(os.path.dirname(os.path.abspath(__file__)))

PORT = 8080
Handler = http.server.SimpleHTTPRequestHandler
Handler.extensions_map.update({'.js': 'application/javascript'})

print(f"Frontend running at http://localhost:{PORT}")
print("Press Ctrl+C to stop.")
with socketserver.TCPServer(("", PORT), Handler) as httpd:
    httpd.serve_forever()
