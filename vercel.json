// Vercel serverless proxy — fetches from Apps Script server-side (no CORS)

const AS_URL = 'https://script.google.com/macros/s/AKfycbyq687XCjn2YbtAjLHvrWE0RZ5u-Tyijhoa_5nWHkJorE62_G1xYLEvEmx3dTA9Zs9k/exec?action=eventos';

module.exports = async function handler(req, res) {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Cache-Control', 's-maxage=120, stale-while-revalidate=60');

  try {
    const response = await fetch(AS_URL, {
      redirect: 'follow',
      headers: {
        'User-Agent': 'Mozilla/5.0 (compatible; ArecoEventos/1.0)',
        'Accept': 'application/json, text/plain, */*',
      },
    });

    // Read as text first to safely handle non-JSON responses
    const text = await response.text();

    // Try to parse as JSON
    let data;
    try {
      data = JSON.parse(text);
    } catch (parseErr) {
      // Apps Script returned HTML or non-JSON — return preview for debugging
      return res.status(502).json({
        error: 'Apps Script returned non-JSON',
        status: response.status,
        preview: text.slice(0, 300),
      });
    }

    return res.status(200).json(data);

  } catch (err) {
    return res.status(500).json({
      error: err.message,
      type: err.constructor.name,
    });
  }
};
