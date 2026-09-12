/**
 * Staff App (ADR-0037): 店舗（スタッフ）専用タッチ front-end。player 用 `mobile/` とは別アプリ。
 *
 * repository は StaffRepository interface 越しに注入する (contract-first, ADR-0037 §6):
 * - EXPO_PUBLIC_API_URL が設定されていれば HttpStaffRepository (staff API, ADR-0021)
 *   例: EXPO_PUBLIC_API_URL=http://192.168.1.10:8788 npx expo start
 * - 未設定なら MockStaffRepository (契約 fixtures 相当の in-memory データ)
 *
 * navigation は依存を増やさない最小 stack (useState)。
 * 画面: Login → SessionList → TableView（会計 / 注文タブ）。
 * 座席 / ハンドロガー遠隔制御タブは ADR-0038 の staff API 追加後に additive で足す。
 */
import { StatusBar } from "expo-status-bar";
import React, { useMemo, useState } from "react";
import { View } from "react-native";

import { HttpStaffRepository } from "./src/api/httpRepository";
import { MockStaffRepository } from "./src/api/mockRepository";
import type { StaffRepository } from "./src/api/repository";
import type { StaffSession } from "./src/api/types";
import { colors } from "./src/screens/common";
import { LoginScreen } from "./src/screens/LoginScreen";
import { SessionListScreen } from "./src/screens/SessionListScreen";
import { TableViewScreen } from "./src/screens/TableViewScreen";

type Route =
  | { name: "login" }
  | { name: "sessions" }
  | { name: "table"; session: StaffSession };

export default function App(): React.JSX.Element {
  const repository: StaffRepository = useMemo(() => {
    const apiUrl = process.env.EXPO_PUBLIC_API_URL;
    return apiUrl ? new HttpStaffRepository(apiUrl) : new MockStaffRepository();
  }, []);

  const [route, setRoute] = useState<Route>({ name: "login" });

  return (
    <View style={{ flex: 1, backgroundColor: colors.bg }}>
      <StatusBar style="light" />
      {route.name === "login" && (
        <LoginScreen
          repository={repository}
          onAuthed={() => setRoute({ name: "sessions" })}
        />
      )}
      {route.name === "sessions" && (
        <SessionListScreen
          repository={repository}
          onSelect={(session) => setRoute({ name: "table", session })}
          onLogout={() => {
            repository.clearToken();
            setRoute({ name: "login" });
          }}
        />
      )}
      {route.name === "table" && (
        <TableViewScreen
          repository={repository}
          session={route.session}
          onBack={() => setRoute({ name: "sessions" })}
        />
      )}
    </View>
  );
}
