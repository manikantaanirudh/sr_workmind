import { NextRequest, NextResponse } from "next/server";

export const runtime = "nodejs";

function getBackendBaseUrl(): string {
  const raw = (process.env.BACKEND_API_BASE_URL ?? "http://127.0.0.1:8000").trim();
  const withoutTrailingSlash = raw.replace(/\/$/, "");
  if (/^https?:\/\//i.test(withoutTrailingSlash)) {
    return withoutTrailingSlash;
  }
  return `http://${withoutTrailingSlash}`;
}

async function proxy(request: NextRequest, pathSegments: string[]) {
  const path = pathSegments.join("/");
  const search = request.nextUrl.search;
  const target = `${getBackendBaseUrl()}/${path}${search}`;

  const headers = new Headers(request.headers);
  headers.delete("host");

  const init: RequestInit = {
    method: request.method,
    headers,
    redirect: "manual",
  };

  if (request.method !== "GET" && request.method !== "HEAD") {
    init.body = await request.arrayBuffer();
  }

  const controller = new AbortController();
  const timeoutMs = 180_000;
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

  const fetchUpstream = () =>
    fetch(target, {
      ...init,
      signal: controller.signal,
    });

  try {
    let upstream = await fetchUpstream();
    // Render free tier: backend may 502 while waking — retry once after a short pause.
    if (upstream.status === 502 || upstream.status === 503) {
      await new Promise((resolve) => setTimeout(resolve, 8000));
      upstream = await fetchUpstream();
    }

    if (upstream.status >= 300 && upstream.status < 400) {
      const location = upstream.headers.get("location");
      if (location) {
        return NextResponse.redirect(location, upstream.status);
      }
    }

    const bodyText = await upstream.text();
    // Do not forward content-encoding/content-length from upstream: body is already decoded.
    const responseHeaders = new Headers();
    const contentType = upstream.headers.get("content-type");
    if (contentType) {
      responseHeaders.set("content-type", contentType);
    }

    return new NextResponse(bodyText, {
      status: upstream.status,
      statusText: upstream.statusText,
      headers: responseHeaders,
    });
  } catch (error) {
    const message =
      error instanceof Error ? error.message : "Failed to reach backend service";
    const timedOut = error instanceof Error && error.name === "AbortError";
    return NextResponse.json(
      {
        detail: timedOut
          ? `Backend request timed out after ${timeoutMs / 1000}s (${getBackendBaseUrl()}).`
          : `Backend proxy error (${getBackendBaseUrl()}): ${message}`,
      },
      { status: timedOut ? 504 : 502 },
    );
  } finally {
    clearTimeout(timeoutId);
  }
}

type RouteContext = { params: Promise<{ path: string[] }> };

async function handle(request: NextRequest, context: RouteContext) {
  const { path } = await context.params;
  return proxy(request, path);
}

export const GET = handle;
export const POST = handle;
export const PUT = handle;
export const PATCH = handle;
export const DELETE = handle;
export const HEAD = handle;
export const OPTIONS = handle;
