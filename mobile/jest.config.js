/**
 * The current tests target the pure-TS layers (repositories, validation, models)
 * which have no react-native imports, so the jest-expo preset is enough without
 * extra native mocks. Screen render/smoke tests can be layered on later with
 * @testing-library/react-native (out of scope for this scaffold).
 */
module.exports = {
  preset: 'jest-expo',
  testMatch: ['**/__tests__/**/*.test.ts', '**/__tests__/**/*.test.tsx'],
  transformIgnorePatterns: [
    'node_modules/(?!((jest-)?react-native|@react-native(-community)?|expo(nent)?|@expo(nent)?/.*|@react-navigation/.*))',
  ],
};
