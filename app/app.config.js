// Extends app.json so a build can target a different App Store Connect app
// (for example the Fastlane build in fastlane/Fastfile) without editing
// app.json. Unset, app.json is used unchanged.
//   IOS_BUNDLE_ID  replaces ios.bundleIdentifier
//   IOS_APP_GROUP  replaces the share extension's app group
//                  (expo-share-intent defaults to group.<bundle id>)
module.exports = ({ config }) => {
  const bundleIdentifier = process.env.IOS_BUNDLE_ID;
  const appGroup = process.env.IOS_APP_GROUP;
  if (!bundleIdentifier && !appGroup) return config;

  const plugins = (config.plugins || []).map((plugin) => {
    const [name, params] = Array.isArray(plugin) ? plugin : [plugin, {}];
    if (name !== "expo-share-intent" || !appGroup) return plugin;
    return [name, { ...params, iosAppGroupIdentifier: appGroup }];
  });

  return {
    ...config,
    ios: { ...config.ios, ...(bundleIdentifier && { bundleIdentifier }) },
    plugins,
  };
};
