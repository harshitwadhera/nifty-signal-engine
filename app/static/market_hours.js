(() => {
  const formatter = new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Asia/Kolkata',
    weekday: 'short',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23'
  });
  const weekdays = new Set(['Mon', 'Tue', 'Wed', 'Thu', 'Fri']);

  function istParts(now = new Date()) {
    const values = {};
    for (const part of formatter.formatToParts(now)) {
      if (part.type !== 'literal') values[part.type] = part.value;
    }
    return values;
  }

  function marketAutoRefreshAllowed(now = new Date()) {
    const parts = istParts(now);
    if (!weekdays.has(parts.weekday)) return false;
    const seconds = Number(parts.hour) * 3600 + Number(parts.minute) * 60 + Number(parts.second);
    return seconds >= 9 * 3600 && seconds < (15 * 3600 + 40 * 60);
  }

  window.marketAutoRefreshAllowed = marketAutoRefreshAllowed;
})();
