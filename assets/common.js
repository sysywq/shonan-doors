/* ==========================================================================
   湘南Doors 共通JS (Phase 1で切り出し)
   トップページ・記事個別ページ双方から読み込まれる。
   - SNSフォローアイコンの描画
   - プライバシーポリシー/お問い合わせ/運営者情報の静的モーダル
   ========================================================================== */

const SITE_TITLE = document.title;
const overlay = document.getElementById('overlay');
const modal = document.getElementById('modal');

/* ---------- SNS follow icons ----------
   下記の href="#" を、実際にSNSアカウントを開設した時点で
   本物のURL（例: https://www.instagram.com/shonan_doors ）に置き換えてください。
*/
const SNS_ICONS = [
  {name:'Instagram', href:'#', bg:'linear-gradient(45deg,#FEDA75,#FA7E1E,#D62976,#962FBF,#4F5BD5)', path:'M7 2h10a5 5 0 0 1 5 5v10a5 5 0 0 1-5 5H7a5 5 0 0 1-5-5V7a5 5 0 0 1 5-5zm0 2a3 3 0 0 0-3 3v10a3 3 0 0 0 3 3h10a3 3 0 0 0 3-3V7a3 3 0 0 0-3-3H7zm5 3.5A4.5 4.5 0 1 1 7.5 12 4.5 4.5 0 0 1 12 7.5zm0 2A2.5 2.5 0 1 0 14.5 12 2.5 2.5 0 0 0 12 9.5zM17.8 6.2a1.1 1.1 0 1 1-1.1 1.1 1.1 1.1 0 0 1 1.1-1.1z'},
  {name:'TikTok', href:'#', bg:'#000000', path:'M14 3h2.2a4.6 4.6 0 0 0 3.8 3.9V9a7 7 0 0 1-3.8-1.1v6.6a5.5 5.5 0 1 1-5.5-5.5c.2 0 .4 0 .6.03v2.2a3.3 3.3 0 1 0 2.3 3.15V3z'},
  {name:'X', href:'#', bg:'#000000', path:'M4 4l7 8.5L4.5 20H7l5-5.8L16 20h4l-7.3-8.9L19.5 4H17l-4.6 5.3L8 4H4z'},
  {name:'Facebook', href:'#', bg:'#1877F2', path:'M14 9h3V6h-3c-2.2 0-4 1.8-4 4v2H8v3h2v6h3v-6h3l1-3h-4v-2c0-.6.4-1 1-1z'},
  {name:'YouTube', href:'#', bg:'#FF0000', path:'M21.6 7.2a2.7 2.7 0 0 0-1.9-1.9C18 5 12 5 12 5s-6 0-7.7.3A2.7 2.7 0 0 0 2.4 7.2 28 28 0 0 0 2 12a28 28 0 0 0 .4 4.8 2.7 2.7 0 0 0 1.9 1.9C6 19 12 19 12 19s6 0 7.7-.3a2.7 2.7 0 0 0 1.9-1.9A28 28 0 0 0 22 12a28 28 0 0 0-.4-4.8zM10 15V9l5.2 3z'},
  {name:'LINE', href:'#', bg:'#06C755', path:'M12 3C6.5 3 2 6.6 2 11c0 3.9 3.5 7.1 8.3 7.8.3.1.8.2.9.5.1.3.1.7 0 1l-.1 1c0 .3-.2 1 .9.6 1-.4 5.6-3.3 7.6-5.6 1.4-1.5 2.4-3 2.4-5.3C22 6.6 17.5 3 12 3zM8.2 13.4H6.7c-.2 0-.4-.2-.4-.4V9.4c0-.2.2-.4.4-.4h.4c.2 0 .4.2.4.4v3.2h.7c.2 0 .4.2.4.4v.4c0 .2-.2.4-.4.4zm2.1 0h-.4c-.2 0-.4-.2-.4-.4V9.4c0-.2.2-.4.4-.4h.4c.2 0 .4.2.4.4v3.6c0 .2-.2.4-.4.4zm4.3 0h-.4c-.1 0-.2 0-.3-.1l-1.6-2.2v1.9c0 .2-.2.4-.4.4h-.4c-.2 0-.4-.2-.4-.4V9.4c0-.2.2-.4.4-.4h.4c.1 0 .2.1.3.2l1.6 2.2V9.4c0-.2.2-.4.4-.4h.4c.2 0 .4.2.4.4v3.6c0 .2-.2.4-.4.4zm3.6-3.2h-1.5v.6h1.5c.2 0 .4.2.4.4v.4c0 .2-.2.4-.4.4h-1.5v.6h1.5c.2 0 .4.2.4.4v.4c0 .2-.2.4-.4.4h-2.3c-.2 0-.4-.2-.4-.4V9.4c0-.2.2-.4.4-.4h2.3c.2 0 .4.2.4.4v.4c0 .2-.2.4-.4.4z'},
];
const snsRow = document.getElementById('snsRow');
if (snsRow) {
  SNS_ICONS.forEach(s=>{
    const a = document.createElement('a');
    a.className='sns-icon'; a.href=s.href; a.title=s.name; a.target='_blank'; a.rel='noopener';
    a.style.background = s.bg;
    a.innerHTML = `<svg viewBox="0 0 24 24" fill="#fff"><path d="${s.path}"/></svg>`;
    snsRow.appendChild(a);
  });
}

/* ---------- フッターの静的ページ（プライバシーポリシー・お問い合わせ・運営者情報） ---------- */
const STATIC_PAGES = {
  privacy: {
    title: 'プライバシーポリシー',
    html: `
      <h3>個人情報の取り扱いについて</h3>
      <p>湘南Doors運営事務局（以下「当サイト」）は、本サイトのご利用にあたり取得する情報について、以下の通りプライバシーポリシーを定めます。</p>
      <h3>アクセス解析ツールについて</h3>
      <p>当サイトでは、サイトの利用状況を把握するためにGoogleアナリティクスを利用しています。Googleアナリティクスは、Cookieを使用してトラフィックデータを収集しますが、これには個人を特定する情報は含まれません。この機能はCookieを無効にすることで収集を拒否することが可能ですので、お使いのブラウザの設定をご確認ください。この規約に関して、詳しくは<a href="https://marketingplatform.google.com/about/analytics/terms/jp/" target="_blank" rel="noopener">Googleアナリティクス利用規約のページ</a>や<a href="https://policies.google.com/technologies/ads?hl=ja" target="_blank" rel="noopener">Googleポリシーと規約ページ</a>をご覧ください。</p>
      <h3>広告配信について</h3>
      <p>当サイトは、将来的に第三者配信の広告サービスを利用する場合があります。広告配信事業者は、ユーザーの興味に応じた広告を表示するためにCookieを使用することがあります。</p>
      <h3>お問い合わせ先で取得する情報について</h3>
      <p>お問い合わせフォームやメールにてご提供いただいたお名前・メールアドレス等の個人情報は、お問い合わせへの対応、および必要な情報をお伝えする目的にのみ使用し、ご本人の同意なく第三者に提供することはありません。</p>
      <h3>プライバシーポリシーの変更について</h3>
      <p>当サイトは、必要に応じて本ポリシーの内容を変更することがあります。変更後のプライバシーポリシーは、本ページに掲載した時点から効力を生じるものとします。</p>
      <p style="color:var(--ink-faint); font-size:12px;">制定日：2026年9月13日</p>
    `,
  },
  contact: {
    title: 'お問い合わせ',
    html: `
      <h3>お問い合わせ</h3>
      <p>取材のご依頼、掲載情報の誤りのご指摘、広告掲載（松・竹・梅プラン）に関するお問い合わせなど、下記のメールアドレスまでお気軽にご連絡ください。</p>
      <p style="font-size:16px; font-weight:700;"><a href="mailto:info@shonandoors.com">info@shonandoors.com</a></p>
      <p>内容を確認の上、担当より折り返しご連絡いたします。返信までお時間をいただく場合がございますので、あらかじめご了承ください。</p>
    `,
  },
  operator: {
    title: '運営者情報',
    html: `
      <h3>運営者情報</h3>
      <p>運営：湘南Doors運営事務局</p>
      <p>連絡先：<a href="mailto:info@shonandoors.com">info@shonandoors.com</a></p>
      <p>湘南Doorsは、湘南エリアの企業・お店・人・文化・イベント・観光を継続的に取材し、記録として積み重ねていく地域メディアです。「湘南を知る入口」であることを目的に、湘南Doors運営事務局が企画・編集・運営を行っています。</p>
    `,
  },
};

function openStaticModal(key){
  const page = STATIC_PAGES[key];
  if(!page || !modal || !overlay) return;
  modal.innerHTML = `
    <button class="static-modal-close" id="staticModalClose">✕</button>
    <div class="static-modal-body">${page.html}</div>`;
  overlay.classList.add('open');
  overlay.scrollTop = 0;
  document.title = page.title + '｜' + SITE_TITLE;
  document.getElementById('staticModalClose').addEventListener('click', ()=> closeStaticModal());
}
function closeStaticModal(){
  overlay.classList.remove('open');
  document.title = SITE_TITLE;
}
if (overlay) {
  overlay.addEventListener('click', e=>{ if(e.target===overlay) closeStaticModal(); });
  document.addEventListener('keydown', e=>{ if(e.key==='Escape' && overlay.classList.contains('open')) closeStaticModal(); });
}
['footPrivacy','footContact','footOperator'].forEach(id=>{
  const el = document.getElementById(id);
  if (el) el.addEventListener('click', (e)=>{ e.preventDefault(); openStaticModal(id.replace('foot','').toLowerCase()); });
});
