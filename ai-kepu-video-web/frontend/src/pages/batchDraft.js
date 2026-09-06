export const batchDraftKey = 'insightcut:batch-composer:v2'
export function parseTopics(value) {
  const clean=s=>Array.from(s,c=>{const n=c.codePointAt(0);return n===0x3000?' ':n>=0xff01&&n<=0xff5e?String.fromCodePoint(n-0xfee0):c}).join('').replace(/[\u0009-\u000d\u0020]+/g,' ').replace(/^ | $/g,'')
  const topics=String(value||'').split(/\r\n|\n|\r/).map(clean).filter(Boolean),seen=new Set(),duplicates=[]
  for(const t of topics){const k=t.replace(/[A-Z]/g,c=>c.toLowerCase());if(seen.has(k)&&!duplicates.includes(t))duplicates.push(t);seen.add(k)}
  return {topics,duplicates}
}
export function validateBatch(mode,topics,cards) {
  const items=mode==='theme'?parseTopics(topics).topics.map(content=>({content})):cards.map(({name,content})=>({name,content}))
  const errors=[]
  if(items.length<2||items.length>50)errors.push('每批需要 2–50 个项目')
  items.forEach((i,n)=>{if(!i.content?.trim())errors.push(`第 ${n+1} 项内容为空`);if(i.content?.length>(mode==='theme'?100:5000))errors.push(`第 ${n+1} 项超过 ${mode==='theme'?100:5000} 字${mode==='theme'?'，完整文稿请切换文稿模式':''}`)})
  if(mode==='theme'&&parseTopics(topics).duplicates.length)errors.push('存在重复主题，请调整后提交')
  return {items,errors}
}
export async function mapWithConcurrency(items,limit,fn) {
  let index=0
  await Promise.all(Array.from({length:Math.min(limit,items.length)},async()=>{while(index<items.length){const i=index++;await fn(items[i],i)}}))
}
