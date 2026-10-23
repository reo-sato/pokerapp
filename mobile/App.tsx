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
import React, { useCallback, useEffect, useMemo, useState } from "react";
import { View } from "react-native";

import { loadStoredAuth } from "./src/api/authStorage";
import { HttpRepository } from "./src/api/httpRepository";
import { MockRepository } from "./src/api/mockRepository";
import type { ViewerRepository } from "./src/api/repository";
import type { Player, PlayerSessionSummary } from "./src/api/types";
import { AuthScreen } from "./src/screens/AuthScreen";
import { CorrectionScreen } from "./src/screens/CorrectionScreen";
import { takeLinkFromLocation } from "./src/cloudLink";
import { HandDetailScreen } from "./src/screens/HandDetailScreen";
import { LinkScreen } from "./src/screens/LinkScreen";
import { MyHandsScreen } from "./src/screens/MyHandsScreen";
import { MyLedgerScreen } from "./src/screens/MyLedgerScreen";
import { MySessionsScreen } from "./src/screens/MySessionsScreen";
import { OrderScreen } from "./src/screens/OrderScreen";
import { PlayerSelectScreen } from "./src/screens/PlayerSelectScreen";
import { styles } from "./src/screens/common";
import { storageGetJson, storageRemove, storageSetJson } from "./src/storage";

type Route =
  | { name: "players" }
  | { name: "link"; link: string | null }
  | { name: "auth"; mode: "pin" | "oidc"; player?: Player }
  | { name: "sessions"; player: Player }
  | { name: "hands"; player: Player; session: PlayerSessionSummary }
  | { name: "hand"; player: Player; session: PlayerSessionSummary; handId: number }
  | { name: "correct"; player: Player; session: PlayerSessionSummary; handId: number }
  | { name: "ledger"; player: Player; session: PlayerSessionSummary }
  | { name: "order"; player: Player; session: PlayerSessionSummary };

/** 選択した player の保存キー（web 再読込を跨いで名前選択をスキップする）。 */
const SELECTED_PLAYER_KEY = "phv.player";

// クラウド（店外での閲覧, ADR-0067 D5）向けのビルド。名前選びは出さず、店でもらった閲覧リンクで入る。
const CLOUD_MODE = process.env.EXPO_PUBLIC_CLOUD === "1";

/** 保存済み player があればセッション一覧から再開する（stale でも read は無害・戻るで選び直せる）。 */
function initialRoute(): Route {
  if (CLOUD_MODE) {
    const link = takeLinkFromLocation();
    if (link) return { name: "link", link };
    const auth = loadStoredAuth();
    const p = storageGetJson<Player>(SELECTED_PLAYER_KEY);
    if (auth && p && p.player_id === auth.player_id) return { name: "sessions", player: p };
    return { name: "link", link: null };
  }
  const p = storageGetJson<Player>(SELECTED_PLAYER_KEY);
  if (p && typeof p.player_id === "string" && typeof p.display_name === "string") {
    return { name: "sessions", player: p };
  }
  return { name: "players" };
}

// staff 端末（iPad 訂正等）は EXPO_PUBLIC_STAFF_TOKEN を設定すると staff write が有効になる。
// 未設定（お客さん向けの配布）では訂正の導線を出さない（ADR-0059）。
const STAFF_TOKEN = process.env.EXPO_PUBLIC_STAFF_TOKEN || null;
// 会場が本人確認（PIN / LINE・Google）を使わないとき（player_auth=off）は "off" でビルドし、
// ログイン・サインアップの導線を出さない（名前を選ぶだけ, ADR-0059）。
const PLAYER_AUTH_UI = process.env.EXPO_PUBLIC_PLAYER_AUTH !== "off";

export default function App(): React.JSX.Element {
  const repository: ViewerRepository = useMemo(() => {
    // "/" = 画面と同じ PC・同じポートの API（店舗 PC の viewer API が画面も配信する, ADR-0059）。
    const apiUrl = process.env.EXPO_PUBLIC_API_URL;
    return apiUrl
      ? new HttpRepository(apiUrl, CLOUD_MODE ? null : STAFF_TOKEN, { authReads: CLOUD_MODE })
      : new MockRepository();
  }, []);

  const [route, setRoute] = useState<Route>(initialRoute);

  // 開いたままのページで別の閲覧リンクを開いた（# だけが変わった）ときも、そのリンクで入り直す。
  useEffect(() => {
    if (!CLOUD_MODE) return undefined;
    const target = globalThis as {
      addEventListener?: (type: string, fn: () => void) => void;
      removeEventListener?: (type: string, fn: () => void) => void;
    };
    const onHash = (): void => {
      const link = takeLinkFromLocation();
      if (link) setRoute({ name: "link", link });
    };
    target.addEventListener?.("hashchange", onHash);
    return () => target.removeEventListener?.("hashchange", onHash);
  }, []);

  const selectPlayer = useCallback((player: Player): void => {
    storageSetJson(SELECTED_PLAYER_KEY, player);
    setRoute({ name: "sessions", player });
  }, []);
  // 名前選択に戻る = 明示的な「自分」の切替なので保存をやめる（token は AuthScreen 側の責務）。
  const backToPlayers = (): void => {
    storageRemove(SELECTED_PLAYER_KEY);
    if (CLOUD_MODE) {
      repository.clearAuth(); // ログアウト（もう一度見るには店でもらったリンクから）
      setRoute({ name: "link", link: null });
      return;
    }
    setRoute({ name: "players" });
  };

  return (
    <View style={{ flex: 1, backgroundColor: styles.screen.backgroundColor }}>
      <StatusBar style="light" />
      {route.name === "link" && (
        <LinkScreen repository={repository} link={route.link} onAuthed={selectPlayer} />
      )}
      {route.name === "players" && (
        <PlayerSelectScreen
          repository={repository}
          onSelect={selectPlayer}
          onLogin={
            PLAYER_AUTH_UI ? (player) => setRoute({ name: "auth", mode: "pin", player }) : undefined
          }
          onSignup={PLAYER_AUTH_UI ? () => setRoute({ name: "auth", mode: "oidc" }) : undefined}
        />
      )}
      {route.name === "auth" && (
        <AuthScreen
          repository={repository}
          mode={route.mode}
          player={route.player}
          onAuthed={selectPlayer}
          onBack={() => setRoute({ name: "players" })}
        />
      )}
      {route.name === "sessions" && (
        <MySessionsScreen
          repository={repository}
          player={route.player}
          onSelect={(session) => setRoute({ name: "hands", player: route.player, session })}
          onBack={backToPlayers}
          backLabel={CLOUD_MODE ? "ログアウト" : undefined}
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
          onOpenOrder={
            CLOUD_MODE
              ? undefined // 店の外からは注文しない
              : () => setRoute({ name: "order", player: route.player, session: route.session })
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
          onCorrect={
            STAFF_TOKEN
              ? () =>
                  setRoute({
                    name: "correct", player: route.player, session: route.session,
                    handId: route.handId,
                  })
              : undefined
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
