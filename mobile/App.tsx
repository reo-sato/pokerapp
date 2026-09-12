/**
 * Poker Hand Viewer (M2, ADR-0013): player 向け参照アプリ。
 *
 * repository は ViewerRepository interface 越しに注入する (contract-first, ADR-0004):
 * - EXPO_PUBLIC_API_URL が設定されていれば HttpRepository (viewer API, M1)
 *   例: EXPO_PUBLIC_API_URL=http://192.168.1.10:8788 npx expo start
 * - 未設定なら MockRepository (契約 fixtures 相当の in-memory データ)
 *
 * navigation は M2 では依存を増やさない最小 stack (useState)。
 * 画面: PlayerSelect → MySessions → MyHands → HandDetail。
 */
import { StatusBar } from "expo-status-bar";
import React, { useMemo, useState } from "react";
import { View } from "react-native";

import { HttpRepository } from "./src/api/httpRepository";
import { MockRepository } from "./src/api/mockRepository";
import type { ViewerRepository } from "./src/api/repository";
import type { Player, PlayerSessionSummary } from "./src/api/types";
import { AuthScreen } from "./src/screens/AuthScreen";
import { CorrectionScreen } from "./src/screens/CorrectionScreen";
import { HandDetailScreen } from "./src/screens/HandDetailScreen";
import { MyHandsScreen } from "./src/screens/MyHandsScreen";
import { MyLedgerScreen } from "./src/screens/MyLedgerScreen";
import { MySessionsScreen } from "./src/screens/MySessionsScreen";
import { OrderScreen } from "./src/screens/OrderScreen";
import { PlayerSelectScreen } from "./src/screens/PlayerSelectScreen";
import { styles } from "./src/screens/common";

type Route =
  | { name: "players" }
  | { name: "auth"; mode: "pin" | "oidc"; player?: Player }
  | { name: "sessions"; player: Player }
  | { name: "hands"; player: Player; session: PlayerSessionSummary }
  | { name: "hand"; player: Player; session: PlayerSessionSummary; handId: number }
  | { name: "correct"; player: Player; session: PlayerSessionSummary; handId: number }
  | { name: "ledger"; player: Player; session: PlayerSessionSummary }
  | { name: "order"; player: Player; session: PlayerSessionSummary };

export default function App(): React.JSX.Element {
  const repository: ViewerRepository = useMemo(() => {
    const apiUrl = process.env.EXPO_PUBLIC_API_URL;
    // staff 端末（iPad 訂正等）は EXPO_PUBLIC_STAFF_TOKEN を設定すると staff write が有効になる。
    const staffToken = process.env.EXPO_PUBLIC_STAFF_TOKEN ?? null;
    return apiUrl ? new HttpRepository(apiUrl, staffToken) : new MockRepository();
  }, []);

  const [route, setRoute] = useState<Route>({ name: "players" });

  return (
    <View style={{ flex: 1, backgroundColor: styles.screen.backgroundColor }}>
      <StatusBar style="light" />
      {route.name === "players" && (
        <PlayerSelectScreen
          repository={repository}
          onSelect={(player) => setRoute({ name: "sessions", player })}
          onLogin={(player) => setRoute({ name: "auth", mode: "pin", player })}
          onSignup={() => setRoute({ name: "auth", mode: "oidc" })}
        />
      )}
      {route.name === "auth" && (
        <AuthScreen
          repository={repository}
          mode={route.mode}
          player={route.player}
          onAuthed={(player) => setRoute({ name: "sessions", player })}
          onBack={() => setRoute({ name: "players" })}
        />
      )}
      {route.name === "sessions" && (
        <MySessionsScreen
          repository={repository}
          player={route.player}
          onSelect={(session) => setRoute({ name: "hands", player: route.player, session })}
          onBack={() => setRoute({ name: "players" })}
        />
      )}
      {route.name === "hands" && (
        <MyHandsScreen
          repository={repository}
          player={route.player}
          session={route.session}
          onSelect={(hand) =>
            setRoute({
              name: "hand", player: route.player, session: route.session, handId: hand.hand_id,
            })
          }
          onBack={() => setRoute({ name: "sessions", player: route.player })}
          onOpenLedger={() =>
            setRoute({ name: "ledger", player: route.player, session: route.session })
          }
        />
      )}
      {route.name === "ledger" && (
        <MyLedgerScreen
          repository={repository}
          player={route.player}
          session={route.session}
          onBack={() =>
            setRoute({ name: "hands", player: route.player, session: route.session })
          }
          onOpenOrder={() =>
            setRoute({ name: "order", player: route.player, session: route.session })
          }
        />
      )}
      {route.name === "order" && (
        <OrderScreen
          repository={repository}
          player={route.player}
          session={route.session}
          onBack={() =>
            setRoute({ name: "ledger", player: route.player, session: route.session })
          }
        />
      )}
      {route.name === "hand" && (
        <HandDetailScreen
          repository={repository}
          player={route.player}
          session={route.session}
          handId={route.handId}
          onBack={() =>
            setRoute({ name: "hands", player: route.player, session: route.session })
          }
          onCorrect={() =>
            setRoute({
              name: "correct", player: route.player, session: route.session, handId: route.handId,
            })
          }
        />
      )}
      {route.name === "correct" && (
        <CorrectionScreen
          repository={repository}
          session={route.session}
          handId={route.handId}
          onBack={() =>
            setRoute({
              name: "hand", player: route.player, session: route.session, handId: route.handId,
            })
          }
        />
      )}
    </View>
  );
}
