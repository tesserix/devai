import { NextRequest, NextResponse } from "next/server";

const API_INTERNAL_URL =
  process.env.DEVAI_API_INTERNAL_URL || "http://devai-api.devai.svc.cluster.local:8080";

export async function POST(request: NextRequest) {
  try {
    const body = await request.text();
    const upstream = await fetch(`${API_INTERNAL_URL}/chat/api/message`, {
      method: "POST",
      headers: {
        "Content-Type": request.headers.get("content-type") || "application/json",
      },
      body,
      cache: "no-store",
    });

    const responseBody = await upstream.text();
    return new NextResponse(responseBody, {
      status: upstream.status,
      headers: {
        "Content-Type": upstream.headers.get("content-type") || "application/json",
      },
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : "Chat proxy request failed.";
    return NextResponse.json({ detail: message }, { status: 502 });
  }
}
