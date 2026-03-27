"""Dashboard HTML template."""

INDEX_HTML = """<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>DevAI — AI Development Lifecycle</title>
  <link rel="stylesheet" href="/dashboard/static/css/dashboard.css">
  <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='6' fill='%236366f1'/><text x='50%25' y='55%25' font-family='system-ui' font-size='18' font-weight='800' fill='white' text-anchor='middle' dominant-baseline='middle'>D</text></svg>">
</head>
<body>
  <div id="app">
    <div class="login-page">
      <div class="login-card">
        <h2 class="h3">Loading...</h2>
      </div>
    </div>
  </div>
  <div id="toast" class="toast"></div>
  <script src="/dashboard/static/js/dashboard.js"></script>
</body>
</html>
"""
