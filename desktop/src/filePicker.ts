/**
 * Thin glue over the Tauri dialog plugin, kept apart from the pure logic like
 * `sidecarClient.ts`: the native dialog is the untestable boundary.
 */
import { open } from "@tauri-apps/plugin-dialog";

/** Opens the native file picker for a book or subtitle file. Resolves with
 * the chosen path, or null when the person cancels. */
export async function pickSourceFile(): Promise<string | null> {
  const chosen = await open({
    multiple: false,
    directory: false,
    filters: [{ name: "Libros y subtítulos", extensions: ["epub", "srt"] }],
  });
  return typeof chosen === "string" ? chosen : null;
}
