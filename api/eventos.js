// Vercel serverless function — proxies Apps Script to avoid CORS
// Deployed at: /api/eventos (same origin as the page)

const AS_URL = 'https://script.google.com/macros/s/AKfycbyq687XCjn2YbtAjLHvrWE0RZ5u-Tyijhoa_5nWHkJorE62_G1xYLEvEmx3dTA9Zs9k/exec?action=eventos';

export default async function handler(req, res) {
  try {
    const response = await fetch(AS_URL, {
      redirect: 'follow',
      headers: { 'User-Agent': 'ArecoEventos/1.0' },
    });

    if (!response.ok) {
      return res.status(502).json({ error: 'Apps Script returned ' + response.status });
    }

    const data = await response.json();

    // Cache for 2 minutes — reduces Apps Script calls, speeds up page load
    res.setHeader('Cache-Control', 's-maxage=120, stale-while-revalidate=60');
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.status(200).json(data);

  } catch (err) {
    res.status(500).json({ error: err.message });
  }
}
