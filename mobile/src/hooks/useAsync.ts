import { useEffect, useState } from "react";

import { ViewerApiError } from "../api/types";

export interface AsyncState<T> {
  data: T | null;
  loading: boolean;
  /** error-shapes.md の code（not_found 等）。通信失敗等は "network_error"。 */
  errorCode: string | null;
  errorMessage: string | null;
}

/** repository 呼び出しの loading / error(code 分岐) / data を一元化する。 */
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[]): AsyncState<T> {
  const [state, setState] = useState<AsyncState<T>>({
    data: null, loading: true, errorCode: null, errorMessage: null,
  });

  useEffect(() => {
    let cancelled = false;
    setState({ data: null, loading: true, errorCode: null, errorMessage: null });
    fn().then(
      (data) => {
        if (!cancelled) setState({ data, loading: false, errorCode: null, errorMessage: null });
      },
      (err: unknown) => {
        if (cancelled) return;
        if (err instanceof ViewerApiError) {
          setState({ data: null, loading: false, errorCode: err.code, errorMessage: err.message });
        } else {
          setState({
            data: null, loading: false, errorCode: "network_error",
            errorMessage: err instanceof Error ? err.message : String(err),
          });
        }
      },
    );
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return state;
}
