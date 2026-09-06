import './plan-preset-fields.css'
import { useEffect, useRef, useState } from 'react'
import { getVoices, previewVoice, getConfig } from '../api/task'
import { visualStyles, textStyles, ratioOptions } from '../utils/projectDrafts'

export const defaultPreset = { text_style:'知识科普', visual_style:'吉卜力', ratio:'16:9', length:300, voice_type:'', tts_options:{speed_level:'normal'}, subtitle_options:{size:'standard',position:'standard',outline:'standard'}, generation_options:{prompt_concurrency:4,image_concurrency:8,retry_count:2,retry_interval_seconds:5} }
export function templatePreset(t) { return {...structuredClone(defaultPreset), ...structuredClone(t), template_id:t.template_id, template_name:t.name, adjusted:false} }
export function PlanPresetFields({value,onChange,theme=true,disabled=false}) {
  const [voices,setVoices]=useState([]),[preview,setPreview]=useState(''),[error,setError]=useState('')
  useEffect(()=>{let live=true;getVoices().then(v=>{if(live)setVoices(Array.isArray(v)?v:v.items||[])}).catch(()=>{if(live)setError('音色列表读取失败，请刷新重试')});return()=>{live=false}},[])
  const current=useRef({value,onChange});current.current={value,onChange}
  useEffect(()=>{let active=true;if(!value.voice_type)getConfig().then(config=>{
    const t=config.tts||{},provider=t.provider||'doubao',raw=provider==='mimo'?t.mimo?.default_voice:t.default_voice
    if(active&&raw&&!current.current.value.voice_type)current.current.onChange({...current.current.value,voice_type:raw.includes(':')?raw:`${provider}:${raw}`})
  }).catch(()=>{});return()=>{active=false}},[value.voice_type])
  const patch=(k,v)=>onChange({...value,[k]:v,adjusted:true})
  const nested=(k,f,v)=>patch(k,{...value[k],[f]:v})
  const select=(label,key,options)=> <label className="field" key={key}><span>{label}</span><select disabled={disabled} value={value[key]??defaultPreset[key]} onChange={e=>patch(key,e.target.value)}>{options.map(o=><option key={o.value||o} value={o.value||o}>{o.label||o}</option>)}</select></label>
  const listen=async()=>{setError('');try {const r=await previewVoice({voice_type:value.voice_type,tts_options:value.tts_options});setPreview(r.audio_url||r.preview_url||r.url||'')}catch{setError('试听失败，请检查音色及配音配置')}}
  return <div className="plan-preset-fields">
    {select('创作风格','text_style',textStyles)}{select('画面风格','visual_style',visualStyles)}{select('视频比例','ratio',ratioOptions)}
    {theme&&<label className="field"><span>目标字数（0 为自动）</span><input type="number" min="0" max="2000" disabled={disabled} value={value.length??300} onChange={e=>patch('length',Number(e.target.value))}/></label>}
    <label className="field"><span>配音音色</span><select disabled={disabled} value={value.voice_type||''} onChange={e=>patch('voice_type',e.target.value)}><option value="">系统默认音色（创建时保存）</option>{voices.map(v=><option key={v.id||v.voice_type} value={v.id||v.voice_type}>{v.name||v.display_name||v.id}</option>)}</select></label>
    <button type="button" disabled={!value.voice_type||disabled} onClick={listen}>试听当前音色</button>{preview&&<audio controls src={preview}/>}{error&&<p role="alert">{error}</p>}
    <details><summary>配音、字幕和生成策略</summary>
      <label className="field"><span>语速</span><select disabled={disabled} value={value.tts_options?.speed_level||'normal'} onChange={e=>nested('tts_options','speed_level',e.target.value)}>{[['very_slow','很慢'],['slow','偏慢'],['normal','正常'],['fast','偏快'],['very_fast','很快']].map(([v,n])=><option key={v} value={v}>{n}</option>)}</select></label>
      <label className="field"><span>豆包音量</span><input disabled={disabled} type="number" min="0.5" max="2" step="0.1" value={value.tts_options?.volume_ratio??1} onChange={e=>nested('tts_options','volume_ratio',Number(e.target.value))}/></label>
      <label className="field"><span>MiMo 风格指令</span><textarea disabled={disabled} maxLength={300} value={value.tts_options?.style_prompt||''} onChange={e=>nested('tts_options','style_prompt',e.target.value)}/></label>
      {Object.entries({size:['字号',[['small','小'],['standard','标准'],['large','大']]],position:['字幕位置',[['low','偏低'],['standard','标准'],['high','偏高']]],outline:['描边',[['light','轻'],['standard','标准'],['strong','强']]]}).map(([k,[label,opts]])=><label className="field" key={k}><span>{label}</span><select disabled={disabled} value={value.subtitle_options?.[k]||'standard'} onChange={e=>nested('subtitle_options',k,e.target.value)}>{opts.map(([v,n])=><option key={v} value={v}>{n}</option>)}</select></label>)}
      {Object.entries({prompt_concurrency:['提示词并发',1,8],image_concurrency:['生图并发',1,8],retry_count:['失败重试次数',0,5],retry_interval_seconds:['重试间隔（秒）',1,60]}).map(([k,[n,min,max]])=><label className="field" key={k}><span>{n}</span><input disabled={disabled} type="number" min={min} max={max} value={value.generation_options?.[k]??defaultPreset.generation_options[k]} onChange={e=>nested('generation_options',k,Number(e.target.value))}/></label>)}
    </details>
  </div>
}
