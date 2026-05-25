/**
 * Repository error shapes — mirrors docs/contracts/error-shapes.md.
 *
 * Branching is on `code` (stable, machine-readable). `message` is for display
 * only and may be localized / reworded, so the UI must never branch on it.
 *
 * The `code` set here is 1:1 with the core Python exception hierarchy
 * (PlayerValidationError / EmptyDisplayNameError / DuplicateDisplayNameError /
 * PlayerNotFoundError). New codes are additive (see versioning-and-freeze.md).
 */

export type ErrorCode =
  | 'not_found'
  | 'empty_display_name'
  | 'duplicate_display_name'
  | 'validation_error';

export class RepositoryError extends Error {
  readonly code: ErrorCode;
  /** Optional form field the error applies to (e.g. "display_name"). */
  readonly field?: string;

  constructor(code: ErrorCode, message: string, field?: string) {
    super(message);
    this.name = 'RepositoryError';
    this.code = code;
    this.field = field;
    // Restore prototype chain for instanceof across transpilation targets.
    Object.setPrototypeOf(this, RepositoryError.prototype);
  }
}

export function isRepositoryError(value: unknown): value is RepositoryError {
  return value instanceof RepositoryError;
}
