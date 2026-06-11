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
import { HandDetailScreen } from "./src/screens/HandDetailScreen";
import { MyHandsScreen } from "./src/screens/MyHandsScreen";
import { MySessionsScreen } from "./src/screens/MySessionsScreen";
import { PlayerSelectScreen } from "./src/screens/PlayerSelectScreen";
import { styles } from "./src/screens/common";

type Route =
  | { name: "players" }
  | { name: "sessions"; player: Player }
  | { name: "hands"; player: Player; session: PlayerSessionSummary }
  | { name: "hand"; player: Player; session: PlayerSessionSummary; handId: number };

export default function App(): React.JSX.Element {
  const repository: ViewerRepository = useMemo(() => {
    const apiUrl = process.env.EXPO_PUBLIC_API_URL;
    return apiUrl ? new HttpRepository(apiUrl) : new MockRepository();
  }, []);

  const [route, setRoute] = useState<Route>({ name: "players" });

  return (
    <View style={{ flex: 1, backgroundColor: styles.screen.backgroundColor }}>
      <StatusBar style="light" />
      {route.name === "players" && (
        <PlayerSelectScreen
          repository={repository}
          onSelect={(player) => setRoute({ name: "sessions", player })}
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
        />
      )}
    </View>
  );
}
