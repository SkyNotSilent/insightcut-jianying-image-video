import {render,screen,fireEvent,waitFor} from '@testing-library/react'
import {MemoryRouter} from 'react-router'
import {BatchComposer} from './BatchComposer'
import * as api from '../api/task'
vi.mock('../api/task',()=>({createBatch:vi.fn(),createProductionTemplate:vi.fn(),extractDocumentText:vi.fn(),listProductionTemplates:vi.fn(),getVoices:vi.fn(),getConfig:vi.fn(),previewVoice:vi.fn()}))
beforeEach(()=>{localStorage.clear();vi.clearAllMocks();api.listProductionTemplates.mockResolvedValue({items:[]});api.getVoices.mockResolvedValue([]);api.getConfig.mockResolvedValue({});api.createBatch.mockResolvedValue({batch_id:'new'})})
it('submits three multiline scripts unchanged, without consulting single-project settings',async()=>{
 localStorage.setItem('insightcut:last-tts-options',JSON.stringify({speed_level:'fast'}))
 render(<MemoryRouter><BatchComposer onModeChange={()=>{}}/></MemoryRouter>)
 fireEvent.click(screen.getByRole('button',{name:'完整文稿模式'}))
 const content='第一段。\n\n第二段。\n'
 fireEvent.change(screen.getByLabelText('完整正文 1'),{target:{value:content}})
 fireEvent.change(screen.getByLabelText('完整正文 2'),{target:{value:content}})
 fireEvent.click(screen.getByRole('button',{name:'复制文稿 1'}))
 fireEvent.click(screen.getByRole('button',{name:'创建 3 个预案'}))
 await waitFor(()=>expect(api.createBatch).toHaveBeenCalled())
 const body=api.createBatch.mock.calls[0][0]
 expect(body.input_mode).toBe('script');expect(body.items.map(i=>i.content)).toEqual([content,content,content]);expect(body.concurrency).toBe(3);expect(body.tts_options.speed_level).toBe('normal')
})
it('restores mode-specific text and settings after unmount',async()=>{
 const r=render(<MemoryRouter><BatchComposer onModeChange={()=>{}}/></MemoryRouter>)
 fireEvent.change(screen.getByLabelText('批量主题，每行一个'),{target:{value:'一\n二'}})
 fireEvent.change(screen.getByLabelText('同时运行项目数'),{target:{value:'10'}})
 fireEvent.click(screen.getByRole('button',{name:'完整文稿模式'}))
 fireEvent.change(screen.getByLabelText('完整正文 1'),{target:{value:'正文\n\n保留'}})
 r.unmount()
 render(<MemoryRouter><BatchComposer onModeChange={()=>{}}/></MemoryRouter>)
 expect(screen.getByLabelText('完整正文 1')).toHaveValue('正文\n\n保留')
 fireEvent.click(screen.getByRole('button',{name:'主题模式'}))
 expect(screen.getByLabelText('批量主题，每行一个')).toHaveValue('一\n二');expect(screen.getByLabelText('同时运行项目数')).toHaveValue(10)
})
it('imports successful files in order, retains oversized text and reports individual failures',async()=>{
 const long='文'.repeat(5001)
 api.extractDocumentText.mockImplementation(async f=>{if(f.name==='bad.pdf')throw Error('PDF 无法提取');return {text:f.name==='long.txt'?long:'正常\n\n正文'}})
 const {container}=render(<MemoryRouter><BatchComposer onModeChange={()=>{}}/></MemoryRouter>)
 fireEvent.click(screen.getByRole('button',{name:'完整文稿模式'}))
 fireEvent.change(container.querySelector('input[type=file]'),{target:{files:[new File(['x'],'first.txt'),new File(['x'],'bad.pdf'),new File(['x'],'long.txt')]}})
 await waitFor(()=>expect(screen.getByLabelText('完整正文 2')).toHaveValue(long))
 expect(screen.getByLabelText('完整正文 1')).toHaveValue('正常\n\n正文')
 expect(screen.getByText(/bad.pdf：PDF 无法提取/)).toBeInTheDocument()
 expect(screen.getByText(/第 2 项超过 5000 字/)).toBeInTheDocument()
 expect(screen.getByRole('button',{name:'创建 2 个预案'})).toBeDisabled()
 expect(api.createBatch).not.toHaveBeenCalled()
})
