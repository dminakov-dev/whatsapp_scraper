// Person ids in bridge logs: same rule as collector/privacy.py mask_ids().
// A person is a JID on @c.us / @lid / @s.whatsapp.net, also inside group message ids
// (false_<group>@g.us_<key>_<participant>@c.us). Groups and channels are not persons.
const PERSON_JID = /(\d+)@(c\.us|lid|s\.whatsapp\.net)/g;

export function maskIds(text) {
  if (typeof text !== 'string') return text;
  return text.replace(PERSON_JID, (_, digits, server) => {
    const keep = digits.length > 4 ? digits.slice(-4) : '';
    return `${'*'.repeat(digits.length - keep.length)}${keep}@${server}`;
  });
}
