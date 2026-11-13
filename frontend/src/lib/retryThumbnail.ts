export function retryThumbnail(node: HTMLImageElement, initialUrl: string) {
  let url = initialUrl;
  let controller: AbortController | undefined;
  let objectUrl: string | undefined;

  function reset() {
    controller?.abort();
    controller = undefined;
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    objectUrl = undefined;
  }

  async function retry() {
    if (!url || controller || objectUrl) return;
    const request = new AbortController();
    controller = request;
    try {
      // An <img> error does not expose the HTTP status; fetch can distinguish 503 from 404.
      for (let attempt = 0; attempt < 5; attempt++) {
        const response = await fetch(url, { signal: request.signal, cache: 'no-store' });
        if (response.ok) {
          const blob = await response.blob();
          if (!request.signal.aborted) node.src = objectUrl = URL.createObjectURL(blob);
          return;
        }
        if (response.status !== 503 || attempt === 4) return;
        const header = response.headers.get('Retry-After');
        const delay = header === null ? 1000 : Number(header) * 1000;
        await new Promise<void>((resolve) => {
          const done = () => {
            window.clearTimeout(timer);
            request.signal.removeEventListener('abort', done);
            resolve();
          };
          const timer = window.setTimeout(done, Number.isFinite(delay) ? Math.min(5000, Math.max(0, delay)) : 1000);
          request.signal.addEventListener('abort', done, { once: true });
        });
        if (request.signal.aborted) return;
      }
    } catch {
      // Leave the original image state on cancellation or network failure.
    } finally {
      if (controller === request) controller = undefined;
    }
  }

  node.addEventListener('error', retry);
  return {
    update(nextUrl: string) {
      if (nextUrl === url) return;
      reset();
      url = nextUrl;
    },
    destroy() {
      reset();
      node.removeEventListener('error', retry);
    },
  };
}
