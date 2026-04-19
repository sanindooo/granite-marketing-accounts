import { NextResponse } from "next/server";
import { z } from "zod";
import { bulkDismissEmails } from "@/lib/queries/dashboard";

const bulkDismissSchema = z.object({
  msgIds: z.array(z.string().min(1)).min(1).max(100),
  reason: z.enum(["not_invoice", "resolved"]),
});

export async function POST(request: Request) {
  try {
    const body = await request.json();
    const result = bulkDismissSchema.safeParse(body);

    if (!result.success) {
      return NextResponse.json(
        { error: "Invalid request", details: result.error.issues },
        { status: 400 }
      );
    }

    const { msgIds, reason } = result.data;
    const dismissed = bulkDismissEmails(msgIds, reason);

    return NextResponse.json({ success: true, dismissed });
  } catch (error) {
    console.error("Bulk dismiss error:", error);
    return NextResponse.json(
      { error: "Failed to dismiss emails" },
      { status: 500 }
    );
  }
}
