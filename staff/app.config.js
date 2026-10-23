// app.json の上に、店舗 PC から配信するビルドのときだけ置き場所（`/staff`）を足す（ADR-0064）。
// `STAFF_WEB_BASE_URL` は scripts/build_staff_web.py だけが渡す。開発（expo start）と e2e
// （serve.mjs は `/` で配信）では空のまま = 従来どおり。
module.exports = ({ config }) => {
  const baseUrl = process.env.STAFF_WEB_BASE_URL;
  if (!baseUrl) {
    return config;
  }
  return {
    ...config,
    experiments: { ...(config.experiments ?? {}), baseUrl },
  };
};
