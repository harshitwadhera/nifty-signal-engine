document.getElementById('next-replay').addEventListener('click', async () => {
  const response = await fetch('/api/trades/replay/next', {method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  const row = await response.json();
  document.getElementById('replay-step').textContent = `Step ${row.step}: ${row.price} ${row.stale ? 'STALE — NO EXIT' : 'FRESH'}`;
});
