import { google, type Auth } from "googleapis";
import { readFileSync, existsSync } from "fs";
import { resolve } from "path";
import type { Readable } from "stream";

const CREDENTIALS_PATH = resolve(process.cwd(), "..", "credentials.json");
const TOKEN_PATH = resolve(process.cwd(), "..", ".state", "token.json");

let cachedAuthClient: Auth.OAuth2Client | null = null;

function getAuthClient(): Auth.OAuth2Client {
  if (cachedAuthClient) {
    return cachedAuthClient;
  }

  if (!existsSync(CREDENTIALS_PATH)) {
    throw new Error("credentials.json not found");
  }
  if (!existsSync(TOKEN_PATH)) {
    throw new Error("token.json not found - run `granite ops setup-sheets`");
  }

  const credentials = JSON.parse(readFileSync(CREDENTIALS_PATH, "utf8"));
  const token = JSON.parse(readFileSync(TOKEN_PATH, "utf8"));

  const oauth2 = new google.auth.OAuth2(
    credentials.installed.client_id,
    credentials.installed.client_secret,
    credentials.installed.redirect_uris[0]
  );
  oauth2.setCredentials(token);
  cachedAuthClient = oauth2;
  return oauth2;
}

export async function downloadFileFromDrive(
  fileId: string
): Promise<Readable> {
  const auth = getAuthClient();
  const drive = google.drive({ version: "v3", auth });

  const response = await drive.files.get(
    { fileId, alt: "media" },
    { responseType: "stream" }
  );

  return response.data as Readable;
}

export async function getFileMetadata(fileId: string) {
  const auth = getAuthClient();
  const drive = google.drive({ version: "v3", auth });

  const response = await drive.files.get({
    fileId,
    fields: "name,mimeType,size",
  });

  return response.data;
}
