import test from 'node:test'
import assert from 'node:assert/strict'
import {validateBatch,mapWithConcurrency} from '../src/pages/batchDraft.js'
test('three multiline manuscripts remain three exact inputs, including duplicates',()=>{
 const content='  第一段。\n\n第二段。\n'
 const r=validateBatch('script','',[{name:'一',content},{name:'二',content},{name:'三',content}])
 assert.deepEqual(r.errors,[]);assert.equal(r.items.length,3);assert.ok(r.items.every(i=>i.content===content))
})
test('50 topics are supported and long scripts are rejected without truncation',()=>{
 assert.equal(validateBatch('theme',Array.from({length:50},(_,i)=>`主题${i}`).join('\n'),[]).errors.length,0)
 const content='文'.repeat(5001),r=validateBatch('script','',[{content},{content:'短'}])
 assert.ok(r.errors.length);assert.equal(r.items[0].content.length,5001)
})
test('document import executes at most two simultaneous extractions',async()=>{
 let running=0,peak=0;const done=[]
 await mapWithConcurrency([1,2,3,4,5],2,async item=>{peak=Math.max(peak,++running);await new Promise(r=>setTimeout(r,5));running--;done.push(item)})
 assert.equal(peak,2);assert.deepEqual(done.sort(),[1,2,3,4,5])
})
