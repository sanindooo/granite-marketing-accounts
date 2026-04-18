import { notFound } from "next/navigation";
import Link from "next/link";
import { getInvoiceById } from "@/lib/queries/invoices";
import { PDFViewer } from "@/components/pdf-viewer";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

interface Props {
  params: Promise<{ id: string }>;
}

function formatCurrency(amount: string | null, currency: string): string {
  if (!amount) return "-";
  const num = parseFloat(amount);
  return new Intl.NumberFormat("en-GB", {
    style: "currency",
    currency: currency || "GBP",
  }).format(num);
}

function formatDate(dateStr: string): string {
  return new Date(dateStr).toLocaleDateString("en-GB", {
    weekday: "long",
    day: "numeric",
    month: "long",
    year: "numeric",
  });
}

export default async function InvoiceDetailPage({ params }: Props) {
  const { id } = await params;
  const invoice = getInvoiceById(id);

  if (!invoice) {
    notFound();
  }

  const metadata = [
    { label: "Invoice Number", value: invoice.invoice_number },
    { label: "Date", value: formatDate(invoice.invoice_date) },
    { label: "Vendor", value: invoice.vendor_name || invoice.vendor_name_raw },
    {
      label: "Amount",
      value: formatCurrency(invoice.amount_gross, invoice.currency),
    },
    ...(invoice.currency !== "GBP" && invoice.amount_gross_gbp
      ? [{ label: "Amount (GBP)", value: formatCurrency(invoice.amount_gross_gbp, "GBP") }]
      : []),
    ...(invoice.amount_net
      ? [{ label: "Net", value: formatCurrency(invoice.amount_net, invoice.currency) }]
      : []),
    ...(invoice.amount_vat
      ? [{ label: "VAT", value: formatCurrency(invoice.amount_vat, invoice.currency) }]
      : []),
    ...(invoice.vat_rate ? [{ label: "VAT Rate", value: `${invoice.vat_rate}%` }] : []),
    ...(invoice.vat_number_supplier
      ? [{ label: "Supplier VAT #", value: invoice.vat_number_supplier }]
      : []),
  ];

  return (
    <div className="mx-auto max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
      <div className="mb-6">
        <Link href="/invoices">
          <Button variant="ghost" size="sm" className="mb-4">
            ← Back to invoices
          </Button>
        </Link>
        <div className="flex items-center gap-3">
          <h1 className="text-2xl font-semibold">
            {invoice.vendor_name || invoice.vendor_name_raw}
          </h1>
          <Badge variant="secondary" className="capitalize">
            {invoice.category}
          </Badge>
          {invoice.reverse_charge === 1 && (
            <Badge variant="outline">Reverse Charge</Badge>
          )}
        </div>
        <p className="mt-1 text-muted-foreground">
          Invoice #{invoice.invoice_number}
        </p>
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Details</CardTitle>
          </CardHeader>
          <CardContent>
            <dl className="space-y-3">
              {metadata.map(({ label, value }) => (
                <div key={label} className="flex justify-between">
                  <dt className="text-muted-foreground">{label}</dt>
                  <dd className="font-medium">{value}</dd>
                </div>
              ))}
            </dl>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>PDF</CardTitle>
          </CardHeader>
          <CardContent>
            <PDFViewer
              driveWebViewLink={invoice.drive_web_view_link}
              driveFileId={invoice.drive_file_id}
            />
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
