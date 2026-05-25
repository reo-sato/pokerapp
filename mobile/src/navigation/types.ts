/**
 * Navigation graph for the player registry. Kept tiny on purpose — this is the
 * skeleton future ledger / session / settlement screens will extend.
 */
export type RootStackParamList = {
  PlayerList: undefined;
  AddPlayer: undefined;
  RenamePlayer: { playerId: string; currentDisplayName: string };
};
