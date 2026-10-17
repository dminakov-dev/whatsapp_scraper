// Channel invite code from whatsapp-web.js Channel.channelMetadata (a serialized
// newsletterMetadata). Field names are not confirmed on a live session yet, so accept both
// a bare code and a full https://whatsapp.com/channel/<code> link.
export function inviteCodeOf(chat) {
  const meta = chat?.channelMetadata;
  if (!meta) return null;
  if (typeof meta.inviteCode === 'string' && meta.inviteCode) return meta.inviteCode;
  const link = meta.inviteLink || meta.invite_link;
  const m = typeof link === 'string' && /whatsapp\.com\/channel\/([^/?#]+)/.exec(link);
  return m ? m[1] : null;
}
