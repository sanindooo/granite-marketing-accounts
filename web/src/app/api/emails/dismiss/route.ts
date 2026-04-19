import { NextResponse } from "next/server";
import { z } from "zod";
import { dismissEmail } from "@/lib/queries/dashboard";

const dismissSchema = z.object({
  msgId: z.string().min(1),
  reason: z.enum(["not_invoice", "resolved", "duplicate"]),
  blockDomain: z.boolean().optional(),
});

export async function POST(request: Request) {
  try {
    const body = await request.json();
    const result = dismissSchema.safeParse(body);

    if (!result.success) {
      return NextResponse.json(
        { error: "Invalid request", details: result.error.issues },
        { status: 400 }
      );
    }

    const { msgId, reason, blockDomain: shouldBlockDomain } = result.data;
    const domainBlocked = dismissEmail(msgId, reason, shouldBlockDomain);

    return NextResponse.json({ success: true, domainBlocked });
  } catch (error) {
    console.error("Dismiss error:", error);
    return NextResponse.json(
      { error: "Failed to dismiss email" },
      { status: 500 }
    );
  }
}
