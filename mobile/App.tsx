import React from 'react';
import { StatusBar } from 'expo-status-bar';
import { DarkTheme, NavigationContainer } from '@react-navigation/native';
import { createNativeStackNavigator } from '@react-navigation/native-stack';
import { RootStackParamList } from './src/navigation/types';
import { RepositoryProvider } from './src/context/RepositoryContext';
import { PlayerListScreen } from './src/screens/PlayerListScreen';
import { AddPlayerScreen } from './src/screens/AddPlayerScreen';
import { RenamePlayerScreen } from './src/screens/RenamePlayerScreen';
import { theme } from './src/theme/theme';

const Stack = createNativeStackNavigator<RootStackParamList>();

const navTheme = {
  ...DarkTheme,
  colors: {
    ...DarkTheme.colors,
    background: theme.colors.background,
    card: theme.colors.surface,
    text: theme.colors.text,
    border: theme.colors.border,
    primary: theme.colors.primary,
  },
};

export default function App() {
  return (
    <RepositoryProvider>
      <NavigationContainer theme={navTheme}>
        <StatusBar style="light" />
        <Stack.Navigator initialRouteName="PlayerList">
          <Stack.Screen
            name="PlayerList"
            component={PlayerListScreen}
            options={{ title: 'Player Registry' }}
          />
          <Stack.Screen
            name="AddPlayer"
            component={AddPlayerScreen}
            options={{ title: 'Player を追加' }}
          />
          <Stack.Screen
            name="RenamePlayer"
            component={RenamePlayerScreen}
            options={{ title: 'Player をリネーム' }}
          />
        </Stack.Navigator>
      </NavigationContainer>
    </RepositoryProvider>
  );
}
