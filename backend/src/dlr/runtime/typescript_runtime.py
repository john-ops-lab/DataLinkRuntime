"""TypeScript declarations shared by the compiler and editor contract."""

DECLARATIONS = r"""declare namespace DLR {
  interface InputFile {
    readonly ordinal: number;
    readonly path: string;
    readonly originalName: string;
    readonly contentType: string;
    readonly sizeBytes: number;
    readonly sha256: string;
  }
  interface Context {
    config: Record<string, unknown>;
    readonly inputFiles: readonly InputFile[];
    secrets: { get(key: string): string | null };
    logger: {
      info(...values: unknown[]): void;
      warn(...values: unknown[]): void;
      error(...values: unknown[]): void;
    };
  }
}
declare module "dlr" {
  export type Context = DLR.Context;
  export type InputFile = DLR.InputFile;
}
type Context = DLR.Context;
"""
