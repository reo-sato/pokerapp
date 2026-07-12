import { useCallback, useEffect, useState } from "react";

import { StaffApiError } from "../api/types";

export interface AsyncState<T> {
  data: T | null;
  loading: boolean;
  /** error-shapes.md の code（not_found 等）。通信失敗等は "network_error"。 */
  errorCode: string | null;
  errorMessage: string | null;
}

function toError(err: unknown): { code: string; message: string } {
  if (err instanceof StaffApiError) {
    return { code: err.code, message: err.message };
  }
  return {
    code: "network_error",
    message: err instanceof Error ? err.message : String(err),
  };
}

/** repository 呼び出しの loading / error(code 分岐) / data を一元化する。reload で再取得。 */
export function useAsync<T>(
  fn: () => Promise<T>,
  deps: unknown[],
): AsyncState<T> & { reload: () => void } {
  const [tick, setTick] = useState(0);
  const [state, setState] = useState<AsyncState<T>>({
    data: null, loading: true, errorCode: null, errorMessage: null,
  });
  const reload = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    let cancelled = false;
    // 再取得中も直前の data を保持する（polling でバッジ/一覧がちらつかない。
    // 初回は data=null なので従来どおり Loading になる）。
    setState((prev) => ({ data: prev.data, loading: true, errorCode: null, errorMessage: null }));
    fn().then(
      (data) => {
        if (!cancelled) setState({ data, loading: false, errorCode: null, errorMessage: null });
      },
      (err: unknown) => {
        if (cancelled) return;
        const { code, message } = toError(err);
        setState({ data: null, loading: false, errorCode: code, errorMessage: message });
      },
    );
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);

  return { ...state, reload };
}
