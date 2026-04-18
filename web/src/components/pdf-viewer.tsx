"use client";

const GOOGLE_DRIVE_URL_PATTERN = /^https:\/\/(docs|drive)\.google\.com\//;

interface PDFViewerProps {
  driveWebViewLink: string | null;
  driveFileId: string | null;
}

export function PDFViewer({ driveWebViewLink, driveFileId }: PDFViewerProps) {
  if (!driveWebViewLink) {
    return (
      <div className="flex h-96 items-center justify-center rounded-md border bg-muted">
        <p className="text-muted-foreground">PDF not available</p>
      </div>
    );
  }

  if (!GOOGLE_DRIVE_URL_PATTERN.test(driveWebViewLink)) {
    return (
      <div className="flex h-96 items-center justify-center rounded-md border bg-muted">
        <p className="text-muted-foreground">Invalid PDF link</p>
      </div>
    );
  }

  const embedUrl = driveWebViewLink.replace("/view", "/preview");
  const downloadUrl = driveFileId
    ? `https://drive.google.com/uc?export=download&id=${driveFileId}`
    : null;

  return (
    <div className="space-y-3">
      <iframe
        src={embedUrl}
        className="h-[600px] w-full rounded-md border"
        title="Invoice PDF"
        allowFullScreen
      />
      <div className="flex gap-3 text-sm">
        <a
          href={driveWebViewLink}
          target="_blank"
          rel="noopener noreferrer"
          className="text-blue-600 hover:underline"
        >
          Open in Drive
        </a>
        {downloadUrl && (
          <a
            href={downloadUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="text-blue-600 hover:underline"
          >
            Download PDF
          </a>
        )}
      </div>
    </div>
  );
}
