<!-- src/components/NullbrPage.vue -->
<template>
  <n-layout content-style="padding: 24px;">
    <!-- 顶部标题栏 -->
    <n-page-header title="NULLBR 资源库" subtitle="连接 115 专属资源网络">
      <template #extra>
        <n-space align="center">
          <!-- 顶部简略配额显示 -->
          <n-tooltip trigger="hover" v-if="userProfile">
            <template #trigger>
              <n-tag :type="quotaColor" round :bordered="false" style="cursor: help;">
                <template #icon><n-icon :component="PulseIcon" /></template>
                今日剩余: {{ userProfile.daily_quota - userProfile.daily_used }}
              </n-tag>
            </template>
            <div>
              <div>等级: {{ userProfile.sub_name }}</div>
              <div>今日已用: {{ userProfile.daily_used }} / {{ userProfile.daily_quota }}</div>
              <div>本月已用: {{ userProfile.monthly_used }} / {{ userProfile.monthly_quota }}</div>
            </div>
          </n-tooltip>
          
          <n-button @click="refreshUserInfo" size="small" circle secondary>
            <template #icon><n-icon :component="RefreshIcon" /></template>
          </n-button>

          <n-button @click="showConfig = !showConfig" size="small" secondary>
            <template #icon><n-icon :component="SettingsIcon" /></template>
            配置
          </n-button>
        </n-space>
      </template>
    </n-page-header>

    <!-- 配置面板 -->
    <n-collapse-transition :show="showConfig">
      <n-card :bordered="false" class="dashboard-card" style="margin-top: 16px; margin-bottom: 16px;">
        <template #header>
          <n-space align="center" justify="space-between">
            <span>接入配置</span>
            <n-button tag="a" href="https://nullbr.com/manage" target="_blank" secondary type="primary" size="small">
              <template #icon><n-icon><LinkIcon /></n-icon></template>
              获取 NULLBR Key
            </n-button>
          </n-space>
        </template>
        
        <n-form label-placement="top" :model="config" label-width="auto">
          <n-grid cols="1 1000:2" :x-gap="40" :y-gap="24">
            
            <!-- ================= 左侧：账户与连接 ================= -->
            <n-gi>
              <div class="section-title">
                <n-icon><ServerIcon /></n-icon> 账户与连接
              </div>
              
              <n-form-item label="NULLBR API Key">
                <n-input-group>
                  <n-input v-model:value="config.api_key" type="password" show-password-on="click" placeholder="请输入 API Key" />
                  <n-button type="primary" ghost @click="saveConfigAndRefresh">保存并刷新</n-button>
                </n-input-group>
              </n-form-item>

              <!-- 用户信息卡片 -->
              <div v-if="userProfile" class="user-card">
                <div class="user-card-header">
                  <div class="user-level">
                    <n-tag :type="levelColor" size="small">{{ userProfile.sub_name.toUpperCase() }}</n-tag>
                    <span class="expire-date" v-if="userProfile.expires_at">到期: {{ userProfile.expires_at }}</span>
                    <span class="expire-date" v-else>永久免费</span>
                  </div>
                </div>
                <div class="quota-info">
                  <div class="quota-row">
                    <span>今日配额</span>
                    <span>{{ userProfile.daily_used }} / {{ userProfile.daily_quota }}</span>
                  </div>
                  <n-progress 
                    type="line" 
                    :percentage="Math.min((userProfile.daily_used / userProfile.daily_quota) * 100, 100)" 
                    :color="quotaColorCode"
                    :height="6"
                    :show-indicator="false"
                  />
                  <div class="quota-row" style="margin-top: 8px;">
                    <span>本月配额</span>
                    <span>{{ userProfile.monthly_used }} / {{ userProfile.monthly_quota }}</span>
                  </div>
                  <n-progress 
                    type="line" 
                    :percentage="Math.min((userProfile.monthly_used / userProfile.monthly_quota) * 100, 100)" 
                    status="success"
                    :height="6"
                    :show-indicator="false"
                  />
                </div>
                
                <!-- 兑换码区域 -->
                <n-divider style="margin: 12px 0;" />
                <n-input-group size="small">
                  <n-input v-model:value="redeemCodeInput" placeholder="输入兑换码升级/续费...1-2分钟生效" />
                  <n-button type="primary" secondary @click="handleRedeem" :loading="redeeming">
                    兑换
                  </n-button>
                  <!-- ★★★ 新增：购买按钮 ★★★ -->
                  <n-button type="warning" @click="showBuyModal = true">
                    <template #icon><n-icon><CartIcon /></n-icon></template>
                    购买/续费
                  </n-button>
                </n-input-group>
              </div>
              <n-alert v-else type="warning" style="margin-bottom: 16px;">
                未获取到用户信息。请检查 API Key 是否正确，或检查网络连接。
              </n-alert>

              <n-form-item label="启用数据源">
                <n-checkbox-group v-model:value="config.enabled_sources">
                  <n-space item-style="display: flex;">
                    <n-checkbox value="115" label="115分享" />
                    <n-checkbox value="magnet" label="磁力链" />
                    <n-checkbox value="ed2k" label="电驴(Ed2k)" />
                  </n-space>
                </n-checkbox-group>
              </n-form-item>

              <!-- ★★★ 优化后的 115 与整理模块 (合并紧凑版) ★★★ -->
              <div class="sub-module">
                <!-- 顶部：标题与连通性检查 -->
                <div class="sub-module-header" style="margin-bottom: 8px;">
                  <div style="display: flex; align-items: center; gap: 8px;">
                    <span class="title">115 网盘与整理</span>
                    <!-- 状态提示 (紧凑显示) -->
                    <n-tag v-if="p115Info" type="success" size="small" :bordered="false" round>
                      <template #icon><n-icon :component="CheckIcon" /></template>
                      {{ p115Info.msg || '连接正常' }}
                    </n-tag>
                    <n-tag v-else-if="config.p115_cookies" type="warning" size="small" :bordered="false" round>
                      未检查
                    </n-tag>
                  </div>
                  <n-button size="tiny" secondary type="success" @click="check115Status" :loading="loading115Info">
                    检查连通性
                  </n-button>
                </div>

                <!-- 中间：双列输入框 (Cookie 和 目录) -->
                <n-grid :cols="3" :x-gap="12">
                  <n-gi>
                    <n-form-item label="Cookies" :show-feedback="false">
                      <n-input-group>
                        <n-input 
                          readonly 
                          :value="config.p115_cookies ? '已配置' : ''" 
                          :placeholder="config.p115_cookies ? '' : '未配置'"
                          style="pointer-events: none;"
                        >
                          <template #prefix>
                            <n-icon :color="config.p115_cookies ? '#18a058' : '#ccc'">
                              <component :is="config.p115_cookies ? CheckIcon : CloseIcon" />
                            </n-icon>
                          </template>
                        </n-input>
                        <n-button type="primary" ghost @click="showCookieModal = true">
                          设置
                        </n-button>
                      </n-input-group>
                    </n-form-item>
                  </n-gi>
                  <n-gi>
                    <n-form-item label="待整理目录" :show-feedback="false">
                      <n-input-group>
                        <n-input 
                          :value="config.p115_save_path_name || config.p115_save_path_cid" 
                          placeholder="选择目录" 
                          readonly 
                          @click="openFolderSelector('config', config.p115_save_path_cid)"
                        >
                          <template #prefix><n-icon :component="FolderIcon" /></template>
                        </n-input>
                        <n-button type="primary" ghost @click="openFolderSelector('config', config.p115_save_path_cid)">
                          选择
                        </n-button>
                      </n-input-group>
                    </n-form-item>
                  </n-gi>

                  <!-- 115API请求间隔设置 -->
                  <n-gi>
                    <n-form-item label="API 请求间隔 (秒)">
                      <n-input-group>
                        <n-input-number 
                          v-model:value="config.request_interval" 
                          :min="1" 
                          :step="1" 
                          placeholder="5" 
                          style="width: 100%;"
                        >
                          <template #suffix>秒</template>
                        </n-input-number>
                        <n-tooltip trigger="hover">
                          <template #trigger>
                            <n-button type="warning" ghost>
                              <template #icon><n-icon><AlertIcon /></n-icon></template>
                              风险提示
                            </n-button>
                          </template>
                          115 官方对 API 调用频率有严格限制。<br>
                          默认 5 秒是最安全的。<br>
                          <span style="color: #ff4d4f; font-weight: bold;">谨慎设置</span>
                        </n-tooltip>
                      </n-input-group>
                    </n-form-item>
                  </n-gi>
                </n-grid>

                <n-divider style="margin: 12px 0;" />

                <!-- 底部：整理开关与操作按钮 (Flex 布局) -->
                <div style="display: flex; justify-content: space-between; align-items: center;">
                  <!-- 左侧：开关与说明 -->
                  <div style="display: flex; align-items: center; gap: 12px;">
                    <n-switch v-model:value="config.enable_smart_organize">
                      <template #checked>智能整理: 开启</template>
                      <template #unchecked>智能整理: 关闭</template>
                    </n-switch>
                    <n-text depth="3" style="font-size: 12px;" class="hide-on-mobile">
                      <span v-if="config.enable_smart_organize">接管整理，按右边的分类规则入库</span>
                      <span v-else>仅转存，不执行整理</span>
                    </n-text>
                  </div>

                  <!-- 右侧：操作按钮组 -->
                  <n-space size="small">
                    <!-- ★★★ 新增：立即扫描按钮 ★★★ -->
                    <n-tooltip trigger="hover">
                      <template #trigger>
                        <n-button 
                          secondary 
                          type="info" 
                          @click="handleScanTask" 
                          :loading="scanningTask"
                          :disabled="!config.enable_smart_organize"
                        >
                          <template #icon><n-icon :component="RunIcon" /></template>
                          整理
                        </n-button>
                      </template>
                      立即扫描待整理目录并执行归类
                    </n-tooltip>

                    <!-- 规则管理按钮 -->
                    <n-button secondary @click="showRuleManagerModal = true">
                      <template #icon><n-icon :component="ListIcon" /></template>
                      分类规则 ({{ sortingRules.length }})
                    </n-button>
                  </n-space>
                </div>
              </div>

              <!-- CMS 模块 -->
              <div class="sub-module">
                <div class="sub-module-header">
                  <span class="title">CMS 通知 (可选)</span>
                </div>
                <n-grid :cols="2" :x-gap="12">
                  <n-gi>
                    <n-form-item label="CMS 地址" :show-feedback="false">
                      <n-input v-model:value="config.cms_url" placeholder="http://ip:port" />
                    </n-form-item>
                  </n-gi>
                  <n-gi>
                    <n-form-item label="CMS Token" :show-feedback="false">
                      <n-input v-model:value="config.cms_token" type="password" show-password-on="click" placeholder="Token" />
                    </n-form-item>
                  </n-gi>
                </n-grid>
              </div>
            </n-gi>

            <!-- ================= 右侧：过滤与片单 ================= -->
            <n-gi>
              <div class="section-title">
                <n-icon><FilterIcon /></n-icon> 资源过滤规则
              </div>

              <div class="filter-box">
                <n-grid :cols="2" :x-gap="24">
                  <n-gi>
                    <n-form-item label="分辨率偏好">
                        <n-checkbox-group v-model:value="config.filters.resolutions">
                          <n-space vertical :size="4">
                            <n-checkbox value="2160p" label="4K (2160p)" />
                            <n-checkbox value="1080p" label="1080p" />
                            <n-checkbox value="720p" label="720p" />
                          </n-space>
                        </n-checkbox-group>
                    </n-form-item>
                  </n-gi>
                  <n-gi>
                    <n-form-item label="质量/版本">
                        <n-checkbox-group v-model:value="config.filters.qualities">
                          <n-space vertical :size="4">
                            <n-checkbox value="Remux" label="Remux (原盘)" />
                            <n-checkbox value="HDR10" label="HDR" />
                            <n-checkbox value="Dolby Vision" label="Dolby Vision" />
                            <n-checkbox value="WEB-DL" label="WEB-DL" />
                          </n-space>
                        </n-checkbox-group>
                    </n-form-item>
                  </n-gi>
                </n-grid>
                
                <n-divider style="margin: 12px 0" />

                <n-form-item label="容器格式 (仅电影)">
                  <n-space align="center">
                      <n-checkbox-group v-model:value="config.filters.containers">
                        <n-space>
                          <n-checkbox value="mkv" label="MKV" />
                          <n-checkbox value="mp4" label="MP4" />
                          <n-checkbox value="ts" label="TS" />
                          <n-checkbox value="iso" label="ISO" />
                        </n-space>
                      </n-checkbox-group>
                      <n-divider vertical />
                      <n-switch v-model:value="config.filters.require_zh" size="small">
                        <template #checked>必须含中文字幕</template>
                        <template #unchecked>不限字幕</template>
                      </n-switch>
                  </n-space>
                </n-form-item>

                <n-grid :cols="2" :x-gap="12">
                  <n-gi>
                    <n-form-item label="电影大小 (GB)">
                      <n-input-group>
                        <n-input-number v-model:value="config.filters.movie_min_size" :min="0" placeholder="0" :show-button="false" />
                        <n-input-group-label style="background: transparent; border-left: 0; border-right: 0;">-</n-input-group-label>
                        <n-input-number v-model:value="config.filters.movie_max_size" :min="0" placeholder="∞" :show-button="false" />
                      </n-input-group>
                    </n-form-item>
                  </n-gi>
                  <n-gi>
                    <n-form-item label="剧集大小 (GB/集)">
                      <n-input-group>
                        <n-input-number v-model:value="config.filters.tv_min_size" :min="0" placeholder="0" :show-button="false" />
                        <n-input-group-label style="background: transparent; border-left: 0; border-right: 0;">-</n-input-group-label>
                        <n-input-number v-model:value="config.filters.tv_max_size" :min="0" placeholder="∞" :show-button="false" />
                      </n-input-group>
                    </n-form-item>
                  </n-gi>
                </n-grid>
              </div>

              <div class="section-title" style="margin-top: 24px;">
                <n-icon><ListIcon /></n-icon> 自定义精选片单
              </div>
              
              <div class="preset-container">
                <n-dynamic-input v-model:value="config.presets" :on-create="onCreatePreset">
                  <template #default="{ value }">
                    <div class="preset-item">
                      <n-input v-model:value="value.name" placeholder="片单名称" style="flex: 1;" />
                      <n-input v-model:value="value.id" placeholder="ID" style="width: 100px; text-align: center;" />
                    </div>
                  </template>
                </n-dynamic-input>
              </div>
              <n-space justify="end" style="margin-top: 8px;">
                <n-button size="tiny" tertiary @click="resetPresets">恢复默认推荐</n-button>
              </n-space>

            </n-gi>
          </n-grid>

          <n-divider />
          
          <n-space justify="end">
            <n-button type="primary" size="large" @click="saveConfig" :loading="saving">
              <template #icon><n-icon><SaveIcon /></n-icon></template>
              保存全部配置
            </n-button>
          </n-space>
        </n-form>
      </n-card>
    </n-collapse-transition>

    <!-- 主体内容 Tabs -->
    <n-tabs type="line" animated style="margin-top: 16px;">
      <n-tab-pane name="search" tab="🔍 资源搜索">
        <n-card :bordered="false" class="dashboard-card">
          <n-input-group>
            <n-input v-model:value="searchKeyword" placeholder="输入电影/剧集名称..." @keyup.enter="handleSearch" />
            <n-button type="primary" ghost @click="handleSearch" :loading="searching">
              <template #icon><n-icon :component="SearchIcon" /></template>
              搜索
            </n-button>
          </n-input-group>
          <div style="margin-top: 20px;">
             <n-spin :show="searching">
                <n-empty v-if="!searchResults.length && !searching" description="暂无数据" />
                <div class="responsive-grid">
                  <div v-for="item in searchResults" :key="item.id" class="grid-item">
                      <!-- ★★★ 修改：点击事件调用 handleResourceClick ★★★ -->
                      <MediaCard :item="item" :loading="loadingResourcesId === item.id" @click="handleResourceClick(item)" />
                  </div>
                </div>
             </n-spin>
          </div>
        </n-card>
      </n-tab-pane>
      <n-tab-pane name="lists" tab="✨ 精选片单">
        <n-layout has-sider style="min-height: 600px; background: none;">
          <n-layout-sider width="260" content-style="padding-right: 16px; background: none;" :native-scrollbar="false">
            <n-menu :options="presetMenuOptions" :value="currentListId" @update:value="handleListChange" />
          </n-layout-sider>
          <n-layout-content content-style="padding-left: 4px; background: none;">
            <n-spin :show="loadingList">
              <div v-if="listItems.length > 0">
                <div class="responsive-grid">
                  <div v-for="item in listItems" :key="item.id" class="grid-item">
                    <!-- ★★★ 修改：点击事件调用 handleResourceClick ★★★ -->
                    <MediaCard :item="item" :loading="loadingResourcesId === item.id" @click="handleResourceClick(item)" />
                  </div>
                </div>
                <div style="display: flex; justify-content: center; margin-top: 20px; margin-bottom: 20px;">
                   <n-button v-if="hasMore" @click="loadMoreList" :loading="loadingMore" size="small">加载更多</n-button>
                   <n-text v-else depth="3" style="font-size: 12px;">没有更多了</n-text>
                </div>
              </div>
              <n-empty v-else description="选择一个片单开始浏览" style="margin-top: 100px;" />
            </n-spin>
          </n-layout-content>
        </n-layout>
      </n-tab-pane>
    </n-tabs>

    <!-- 季选择模态框 -->
    <n-modal v-model:show="showSeasonModal" preset="card" title="选择要搜索的季" style="width: 400px; max-width: 90%;">
      <n-spin :show="loadingSeasons">
        <div v-if="seasonList.length === 0 && !loadingSeasons" style="text-align: center; color: #888; padding: 20px;">
          未找到季信息，将搜索整剧
          <div style="margin-top: 10px;">
             <n-button size="small" @click="selectSeasonAndSearch(null)">直接搜索整剧</n-button>
          </div>
        </div>
        
        <n-space vertical v-else>
          <!-- 搜索整剧按钮 -->
          <n-button block secondary style="justify-content: space-between;" @click="selectSeasonAndSearch(null)">
            <span>搜索整剧 (所有季)</span>
          </n-button>
          <n-divider style="margin: 4px 0;" />
          
          <!-- 分季列表 -->
          <n-button 
            v-for="season in seasonList" 
            :key="season.id" 
            block 
            secondary
            style="justify-content: space-between; height: auto; padding: 10px;"
            @click="selectSeasonAndSearch(season)"
          >
            <span>{{ season.name }}</span>
            <n-tag size="small" :bordered="false" type="info">{{ season.episode_count }} 集</n-tag>
          </n-button>
        </n-space>
      </n-spin>
    </n-modal>

    <!-- 购买套餐模态框 -->
    <n-modal v-model:show="showBuyModal" style="width: 900px; max-width: 95%;">
      <n-card :bordered="false" size="huge" role="dialog" aria-modal="true" style="background: #1a1a1a; color: #fff;">
        <template #header>
          <div style="text-align: center; color: #fff; font-size: 24px;">选择订阅计划</div>
        </template>
        
        <div class="pricing-container">
          <!-- 免费版 -->
          <div class="pricing-card free">
            <div class="plan-name">FREE</div>
            <div class="plan-price">免费</div>
            <div class="plan-features">
              <div class="feature"><n-icon color="#67c23a"><CheckIcon /></n-icon> 每日详情页: 50次</div>
              <div class="feature"><n-icon color="#67c23a"><CheckIcon /></n-icon> API每分钟请求: 25/次</div>
              <div class="feature"><n-icon color="#67c23a"><CheckIcon /></n-icon> API配额: 50/日，1,500/月</div>
              <div class="feature"><n-icon color="#67c23a"><CheckIcon /></n-icon> 浏览影视库</div>
              <div class="feature disabled"><n-icon color="#f56c6c"><CloseIcon /></n-icon> 个人列表同步</div>
              <div class="feature disabled"><n-icon color="#f56c6c"><CloseIcon /></n-icon> MCP功能</div>
            </div>
            <n-button disabled block ghost>当前计划</n-button>
          </div>

          <!-- 白银版 -->
          <div class="pricing-card silver">
            <div class="plan-badge">推荐</div>
            <div class="plan-name">SILVER</div>
            <div class="plan-price">¥15 <span class="period">/月</span></div>
            <div class="plan-features">
              <div class="feature"><n-icon color="#67c23a"><CheckIcon /></n-icon> 每日详情页: 200次</div>
              <div class="feature"><n-icon color="#67c23a"><CheckIcon /></n-icon> API每分钟请求: 60/次</div>
              <div class="feature"><n-icon color="#67c23a"><CheckIcon /></n-icon> API配额: 300/日，1万/月</div>
              <div class="feature"><n-icon color="#67c23a"><CheckIcon /></n-icon> 浏览影视库</div>
              <div class="feature"><n-icon color="#67c23a"><CheckIcon /></n-icon> 个人列表同步</div>
              <div class="feature"><n-icon color="#67c23a"><CheckIcon /></n-icon> MCP功能</div>
            </div>
            <n-button type="info" block @click="selectPlan('Silver', 15)">购买兑换码</n-button>
          </div>

          <!-- 黄金版 -->
          <div class="pricing-card golden">
            <div class="plan-name">GOLDEN</div>
            <div class="plan-price">¥25 <span class="period">/月</span></div>
            <div class="plan-features">
              <div class="feature"><n-icon color="#67c23a"><CheckIcon /></n-icon> 每日详情页: 500次</div>
              <div class="feature"><n-icon color="#67c23a"><CheckIcon /></n-icon> API每分钟请求: 100/次</div>
              <div class="feature"><n-icon color="#67c23a"><CheckIcon /></n-icon> API配额: 1000/日，1万/月</div>
              <div class="feature"><n-icon color="#67c23a"><CheckIcon /></n-icon> 浏览影视库</div>
              <div class="feature"><n-icon color="#67c23a"><CheckIcon /></n-icon> 个人列表同步</div>
              <div class="feature"><n-icon color="#67c23a"><CheckIcon /></n-icon> MCP功能</div>
            </div>
            <n-button type="warning" block @click="selectPlan('Golden', 25)">购买兑换码</n-button>
          </div>
        </div>

        <!-- 支付弹层 (覆盖在卡片上) -->
        <n-collapse-transition :show="!!selectedPlanName">
          <div class="payment-overlay" v-if="selectedPlanName">
            <n-divider />
            <div class="payment-box">
              <div class="payment-left">
                <div class="pay-title">扫码支付 <span style="color: #e6a23c">{{ selectedPlanPrice }}元</span></div>
                <div class="pay-desc">请使用微信扫码，备注您的联系方式</div>
                <img src="/img/wechat_pay.png" class="qr-code" alt="收款码" />
              </div>
              <div class="payment-right">
                <div class="step-item">
                  <div class="step-num">1</div>
                  <div class="step-text">扫描左侧二维码支付 <b>{{ selectedPlanPrice }}元</b></div>
                </div>
                <div class="step-item">
                  <div class="step-num">2</div>
                  <div class="step-text">支付成功后，截图保存凭证</div>
                </div>
                <div class="step-item">
                  <div class="step-num">3</div>
                  <div class="step-text">点击下方按钮联系发码</div>
                </div>
                <div class="step-item">
                  <div class="step-num">4</div>
                  <div class="step-text">输入兑换码点击兑换，1-2分钟生效</div>
                </div>
                
                <n-button type="primary" size="large" tag="a" href="https://t.me/hbq0405" target="_blank" style="margin-top: 20px;">
                  <template #icon><n-icon><PaperPlaneIcon /></n-icon></template>
                  联系 TG 发货
                </n-button>
                <n-button size="small" text style="margin-top: 10px; color: #999;" @click="selectedPlanName = null">
                  返回选择其他计划
                </n-button>
              </div>
            </div>
          </div>
        </n-collapse-transition>

      </n-card>
    </n-modal>
    <!-- 分类规则 -->
    <n-modal v-model:show="showRuleModal" preset="card" title="编辑分类规则" style="width: 650px;">
      <n-form label-placement="left" label-width="100">
        <n-form-item label="规则名称">
          <n-input v-model:value="currentRule.name" placeholder="例如：漫威电影宇宙" />
        </n-form-item>
        <n-form-item label="目标目录">
          <n-input-group>
            <n-input 
              :value="currentRule.dir_name || currentRule.cid" 
              readonly 
              placeholder="点击选择目录" 
              @click="openFolderSelector('rule', currentRule.cid)"
            >
              <template #prefix><n-icon :component="FolderIcon" color="#f0a020" /></template>
            </n-input>
            <n-button type="primary" ghost @click="openFolderSelector('rule', currentRule.cid)">
              选择
            </n-button>
          </n-input-group>
        </n-form-item>
        
        <n-divider title-placement="left" style="font-size: 12px; color: #999;">匹配条件 (满足所有勾选条件时命中)</n-divider>
        
        <n-form-item label="媒体类型">
          <n-radio-group v-model:value="currentRule.media_type">
            <n-radio-button value="all">不限</n-radio-button>
            <n-radio-button value="movie">仅电影</n-radio-button>
            <n-radio-button value="tv">仅剧集</n-radio-button>
          </n-radio-group>
        </n-form-item>

        <n-form-item label="类型/风格">
          <n-select v-model:value="currentRule.genres" multiple filterable :options="computedGenreOptions" placeholder="包含任一类型即可" />
        </n-form-item>
        
        <n-form-item label="国家/地区">
          <n-select v-model:value="currentRule.countries" multiple filterable :options="countryOptions" placeholder="包含任一国家即可" />
        </n-form-item>

        <n-form-item label="原始语言">
          <n-select v-model:value="currentRule.languages" multiple filterable :options="languageOptions" placeholder="包含任一语言即可" />
        </n-form-item>

        <n-form-item label="工作室">
          <n-select v-model:value="currentRule.studios" multiple filterable :options="computedStudioOptions" placeholder="包含任一工作室即可 (如: 漫威, 迪斯尼" />
        </n-form-item>

        <n-form-item label="关键词">
           <n-select v-model:value="currentRule.keywords" multiple filterable tag :options="keywordOptions" placeholder="包含任一关键词即可 (如: 丧尸, 漫改)" />
        </n-form-item>

        <n-form-item label="分级">
           <n-select v-model:value="currentRule.ratings" multiple filterable :options="ratingOptions" placeholder="包含任一分级即可 (如: 限制级)" />
        </n-form-item>

        <!-- 年份范围 -->
        <n-form-item label="年份范围">
          <n-input-group>
            <n-input-number v-model:value="currentRule.year_min" :min="1900" :max="2099" placeholder="起始" :show-button="false" style="width: 50%" />
            <n-input-group-label style="background-color: var(--n-action-color); border: 1px solid var(--n-divider-color); border-left: 0; border-right: 0;">至</n-input-group-label>
            <n-input-number v-model:value="currentRule.year_max" :min="1900" :max="2099" placeholder="结束" :show-button="false" style="width: 50%" />
          </n-input-group>
        </n-form-item>

        <!-- 时长范围 -->
        <n-form-item label="时长 (分钟)">
          <n-input-group>
            <n-input-number v-model:value="currentRule.runtime_min" :min="0" placeholder="0" :show-button="false" style="width: 50%" />
            <n-input-group-label style="background-color: var(--n-action-color); border: 1px solid var(--n-divider-color); border-left: 0; border-right: 0;">至</n-input-group-label>
            <n-input-number v-model:value="currentRule.runtime_max" :min="0" placeholder="∞" :show-button="false" style="width: 50%" />
          </n-input-group>
          <template #feedback>电影为总时长，剧集为单集时长</template>
        </n-form-item>

        <n-form-item label="最低评分">
          <n-input-number v-model:value="currentRule.min_rating" :min="0" :max="10" :step="0.1" placeholder="0" style="width: 100%">
            <template #suffix>分</template>
          </n-input-number>
          <template #feedback>TMDb 评分大于等于此值时命中</template>
        </n-form-item>

      </n-form>
      <template #footer>
        <n-space justify="end">
          <n-button @click="showRuleModal = false">取消</n-button>
          <n-button type="primary" @click="confirmSaveRule">保存</n-button>
        </n-space>
      </template>
    </n-modal>
    <!-- 规则管理模态框 -->
    <n-modal 
      v-model:show="showRuleManagerModal" 
      preset="card" 
      title="分类规则管理" 
      style="width: 800px; max-width: 95%; height: 80vh;"
      content-style="display: flex; flex-direction: column; overflow: hidden;" 
    >
      <template #header-extra>
        <n-tag type="warning" size="small" :bordered="false">拖拽可调整优先级</n-tag>
      </template>
      
      <!-- 
         修改说明：
         1. 外层 n-modal 增加了 content-style="display: flex; ..." 
         2. 下面的 div 去掉了 height: 100%，改为 flex: 1，确保占满剩余空间
      -->
      <div style="display: flex; flex-direction: column; flex: 1; min-height: 0;">
        <!-- 规则列表区域 (可滚动) -->
        <div style="flex: 1; overflow-y: auto; padding-right: 4px; margin-bottom: 16px;">
          <div class="rules-container">
            <draggable 
              v-model="sortingRules" 
              item-key="id" 
              handle=".drag-handle" 
              @end="saveSortingRules"
              :animation="200"
            >
              <template #item="{ element: rule }">
                <div class="rule-item">
                  <n-icon class="drag-handle" :component="DragHandleIcon" size="20" />
                  <div class="rule-info">
                    <div style="display: flex; align-items: center; gap: 8px;">
                      <div class="rule-name">{{ rule.name }}</div>
                      <n-tag v-if="!rule.enabled" size="tiny" type="error" :bordered="false">已禁用</n-tag>
                    </div>
                    <div class="rule-desc">
                        <n-tag size="tiny" :bordered="false" type="info">目录: {{ rule.dir_name }}</n-tag>
                        <span style="margin-left: 8px; font-size: 12px; opacity: 0.7;">{{ getRuleSummary(rule) }}</span>
                    </div>
                  </div>
                  <div class="rule-actions">
                    <n-switch v-model:value="rule.enabled" size="small" @update:value="saveSortingRules">
                       <template #checked>开</template>
                       <template #unchecked>关</template>
                    </n-switch>
                    <n-divider vertical />
                    <n-tooltip trigger="hover">
                      <template #trigger>
                        <n-button text size="medium" @click="editRule(rule)"><n-icon :component="EditIcon" color="#18a058" /></n-button>
                      </template>
                      编辑
                    </n-tooltip>
                    <n-tooltip trigger="hover">
                      <template #trigger>
                        <n-button text size="medium" @click="deleteRule(rule)"><n-icon :component="DeleteIcon" color="#d03050" /></n-button>
                      </template>
                      删除
                    </n-tooltip>
                  </div>
                </div>
              </template>
            </draggable>
            
            <n-empty v-if="sortingRules.length === 0" description="暂无规则，请添加" style="margin: 40px 0;" />
          </div>
        </div>

        <!-- 底部操作区 (固定在底部) -->
        <div style="border-top: 1px solid var(--n-divider-color); padding-top: 16px; flex-shrink: 0;">
          <n-button type="primary" dashed block @click="addRule">
            <template #icon><n-icon :component="AddIcon" /></template>
            添加新规则
          </n-button>
        </div>
      </div>
    </n-modal>
    <!-- 115 Cookie 编辑弹窗 -->
    <n-modal v-model:show="showCookieModal" preset="card" title="配置 115 Cookies" style="width: 600px;">
      <n-alert type="info" :show-icon="true" style="margin-bottom: 16px;">
        建议用不大助手选择不常用设备扫码获取。
        <br>格式通常为: UID=...; CID=...; SEID=...
      </n-alert>
      <n-form-item label="Cookies 内容" :show-feedback="false">
        <n-input 
          v-model:value="config.p115_cookies" 
          type="textarea" 
          placeholder="UID=...; CID=...; SEID=..." 
          :rows="8" 
          :autosize="{ minRows: 5, maxRows: 10 }"
        />
      </n-form-item>
      <template #footer>
        <n-space justify="end">
          <n-button @click="showCookieModal = false">取消</n-button>
          <n-button type="primary" @click="showCookieModal = false; check115Status()">确定并检查</n-button>
        </n-space>
      </template>
    </n-modal>
    <!-- D. ★★★ 通用目录选择器 Popover ★★★ -->
    <!-- 使用 NModal 代替 Popover，因为 Popover 在 Modal 里容易层级混乱，且 Modal 更适合复杂操作 -->
    <n-modal v-model:show="showFolderPopover" preset="card" title="选择 115 目录" style="width: 450px;" :bordered="false">
      <div class="folder-browser">
        <!-- 1. 顶部导航栏 -->
        <div class="browser-header">
          <div class="nav-left">
            <n-button text size="small" @click="load115Folders('0')">
              <template #icon><n-icon size="18"><HomeIcon /></n-icon></template>
            </n-button>
            <n-divider vertical />
            <div class="breadcrumbs">
              <span v-if="currentBrowserCid === '0'">根目录</span>
              <template v-else>
                <span class="crumb-item" @click="load115Folders('0')">...</span>
                <span class="separator">/</span>
                <span class="crumb-item current">{{ currentBrowserFolderName }}</span>
              </template>
            </div>
          </div>
          <!-- 新建文件夹按钮 -->
          <n-popover trigger="click" placement="bottom-end" :show="showCreateFolderInput" @update:show="v => showCreateFolderInput = v">
            <template #trigger>
              <n-button size="tiny" secondary type="primary">
                <template #icon><n-icon><AddIcon /></n-icon></template>
                新建
              </n-button>
            </template>
            <div style="padding: 8px; width: 200px;">
              <n-input v-model:value="newFolderName" placeholder="文件夹名称" size="small" @keyup.enter="handleCreateFolder" />
              <n-button block type="primary" size="small" style="margin-top: 8px;" @click="handleCreateFolder">确定</n-button>
            </div>
          </n-popover>
        </div>

        <!-- 2. 文件夹列表 (固定高度 + 滚动条) -->
        <div class="folder-list-container">
          <n-spin :show="loadingFolders">
            <div class="folder-list">
              <n-empty v-if="folderList.length === 0 && !loadingFolders" description="空文件夹" size="small" style="padding: 40px 0;" />
              
              <div 
                v-for="folder in folderList" 
                :key="folder.id" 
                class="folder-item"
                @click="load115Folders(folder.id, folder.name)"
              >
                <div class="folder-icon-wrapper">
                  <n-icon size="22" color="#ffca28"><FolderIcon /></n-icon>
                </div>
                <span class="folder-name">{{ folder.name }}</span>
                <n-icon size="16" color="#ccc"><ChevronRightIcon /></n-icon>
              </div>
            </div>
          </n-spin>
        </div>

        <!-- 3. 底部确认栏 -->
        <div class="browser-footer">
          <div class="current-info">
            <span style="color: #666; font-size: 12px;">已选: {{ currentBrowserFolderName }}</span>
          </div>
          <n-space>
            <n-button size="small" @click="showFolderPopover = false">取消</n-button>
            <n-button type="primary" size="small" @click="confirmFolderSelection">
              确定选择
            </n-button>
          </n-space>
        </div>
      </div>
    </n-modal>
    <!-- 资源选择弹窗 -->
    <NullbrSearchModal ref="nullbrModalRef" />
  </n-layout>
</template>

<script setup>
import { ref, reactive, onMounted, h, defineComponent, computed } from 'vue';
import axios from 'axios';
import { useMessage, NIcon, NTag, NEllipsis, NSpace, NImage, NButton, NText, NDynamicInput, NTooltip, NCheckbox, NCheckboxGroup, NInputNumber, NSwitch, NSpin, NCollapseTransition, NTabs, NTabPane, NModal, NLayout, NLayoutSider, NLayoutContent, NPageHeader, NCard, NAlert, NForm, NFormItem, NGrid, NGi, NDivider, NInput, NInputGroup, NInputGroupLabel, NMenu, NEmpty, NProgress } from 'naive-ui';
import NullbrSearchModal from './modals/NullbrSearchModal.vue';
import { 
  SettingsOutline as SettingsIcon, 
  Search as SearchIcon, 
  ListOutline as ListIcon,
  PulseOutline as PulseIcon,
  RefreshOutline as RefreshIcon,
  ServerOutline as ServerIcon,
  FilterOutline as FilterIcon,
  LinkOutline as LinkIcon,
  SaveOutline as SaveIcon,
  CartOutline as CartIcon,
  CheckmarkCircleOutline as CheckIcon,
  CloseCircleOutline as CloseIcon,
  PaperPlaneOutline as PaperPlaneIcon,
  Menu as DragHandleIcon, 
  CreateOutline as EditIcon, 
  TrashOutline as DeleteIcon, 
  Add as AddIcon,
  FolderOpenOutline as FolderOpenIcon,
  Folder as FolderIcon,
  HomeOutline as HomeIcon,
  ChevronForward as ChevronRightIcon,
  PlayOutline as RunIcon,
  AlertCircleOutline as AlertIcon
} from '@vicons/ionicons5';
import draggable from 'vuedraggable';
const message = useMessage();

// --- 配置相关 ---
const showConfig = ref(false);
const showCookieModal = ref(false);
const config = reactive({
  api_key: '',
  p115_cookies: '',
  p115_save_path_cid: '',
  p115_save_path_name: '',
  request_interval: 5,
  cms_url: '',    
  cms_token: '',
  enabled_sources: ['115', 'magnet', 'ed2k'], 
  presets: [],
  filters: { resolutions: [], qualities: [], containers: [], require_zh: false, movie_min_size: 0, movie_max_size: 0, tv_min_size: 0, tv_max_size: 0 }
});
const scanningTask = ref(false);

const handleScanTask = async () => {
  scanningTask.value = true;
  try {
    // 调用通用的任务执行接口
    const res = await axios.post('/api/tasks/run', { 
      task_name: 'scan-organize-115' 
    });
    
    if (res.data) {
      message.success('已触发后台扫描任务，请稍候查看日志');
    }
  } catch (e) {
    message.error('任务启动失败: ' + (e.response?.data?.error || e.message));
  } finally {
    // 稍微延迟一下 loading 状态，防止闪烁
    setTimeout(() => { scanningTask.value = false; }, 500);
  }
};
// 用户信息与兑换
const userProfile = ref(null);
const redeemCodeInput = ref('');
const redeeming = ref(false);

const quotaColor = computed(() => {
  if (!userProfile.value) return 'default';
  const ratio = (userProfile.value.daily_quota - userProfile.value.daily_used) / userProfile.value.daily_quota;
  if (ratio <= 0) return 'error';
  if (ratio < 0.2) return 'warning';
  return 'success';
});

const quotaColorCode = computed(() => {
  if (!userProfile.value) return '#18a058';
  const ratio = (userProfile.value.daily_quota - userProfile.value.daily_used) / userProfile.value.daily_quota;
  if (ratio <= 0) return '#d03050';
  if (ratio < 0.2) return '#f0a020';
  return '#18a058';
});

const levelColor = computed(() => {
    const name = userProfile.value?.sub_name?.toLowerCase() || '';
    if (name.includes('gold')) return 'warning';
    if (name.includes('silver')) return 'info';
    return 'default';
});

const saving = ref(false);
const p115Info = ref(null);
const loading115Info = ref(false);

const check115Status = async () => {
    if (!config.p115_cookies) return;
    loading115Info.value = true;
    try {
        const res = await axios.get('/api/nullbr/115/status');
        if (res.data && res.data.data) p115Info.value = res.data.data;
    } catch (e) { p115Info.value = null; } finally { loading115Info.value = false; }
};

const refreshUserInfo = async () => {
    if (!config.api_key) return;
    try {
        const res = await axios.get('/api/nullbr/user/info');
        if (res.data && res.data.data) {
            userProfile.value = res.data.data;
        }
    } catch (e) {
        userProfile.value = null;
        const errMsg = e.response?.data?.message || e.message;
        if (e.response?.status === 401 || e.response?.status === 403) {
             message.error("API Key 无效，请检查");
        } else {
             message.warning(`获取用户信息失败: ${errMsg}`);
        }
    }
};

const handleRedeem = async () => {
    if (!redeemCodeInput.value) return;
    redeeming.value = true;
    try {
        const res = await axios.post('/api/nullbr/user/redeem', { code: redeemCodeInput.value });
        if (res.data && res.data.status === 'success') {
            message.success(res.data.data.message || '兑换成功');
            redeemCodeInput.value = '';
            refreshUserInfo(); // 刷新信息
        } else {
            message.error(res.data.message || '兑换失败');
        }
    } catch (e) {
        message.error(e.response?.data?.message || '请求失败');
    } finally {
        redeeming.value = false;
    }
};

const loadConfig = async () => {
  try {
    const res = await axios.get('/api/nullbr/config');
    if (res.data) {
      Object.assign(config, res.data);
      if (!config.p115_save_path_name && config.p115_save_path_cid) {
          config.p115_save_path_name = config.p115_save_path_cid; 
      }
    }
    const resPresets = await axios.get('/api/nullbr/presets');
    if (resPresets.data) config.presets = resPresets.data;
    
    // 加载完配置后，获取用户信息
    refreshUserInfo();
  } catch (error) {}
  if (config.p115_cookies) check115Status();
};

const saveConfig = async () => {
  saving.value = true;
  try {
    await axios.post('/api/nullbr/config', config);
    await axios.post('/api/nullbr/presets', { presets: config.presets });
    message.success('全部配置已保存');
    showConfig.value = false;
    loadPresets(); 
  } catch (error) { message.error('保存失败'); } finally { saving.value = false; }
  check115Status();
};

const saveConfigAndRefresh = async () => {
    await axios.post('/api/nullbr/config', config);
    message.success('API Key 已保存');
    refreshUserInfo();
};

const onCreatePreset = () => ({ name: '', id: '' });
const resetPresets = async () => {
  try {
    const res = await axios.delete('/api/nullbr/presets');
    if (res.data && res.data.data) {
      config.presets = res.data.data; 
      presetLists.value = res.data.data;
      presetMenuOptions.value = res.data.data.map(list => ({ label: list.name, key: list.id, icon: () => h(NIcon, null, { default: () => h(ListIcon) }) }));
      message.success('已恢复默认片单');
    }
  } catch (error) { message.error('重置失败'); }
};

// --- 搜索与列表 (保持原有逻辑) ---
const searchKeyword = ref('');
const searching = ref(false);
const searchResults = ref([]);
const presetLists = ref([]);
const currentListId = ref(null);
const listItems = ref([]);
const loadingList = ref(false);
const listPage = ref(1);
const hasMore = ref(true);
const loadingMore = ref(false);
const presetMenuOptions = ref([]);

const handleSearch = async () => {
  if (!searchKeyword.value) return;
  searching.value = true;
  searchResults.value = [];
  try {
    const res = await axios.post('/api/nullbr/search', { keyword: searchKeyword.value, page: 1 });
    if (res.data && res.data.data && res.data.data.list) {
      searchResults.value = res.data.data.list.map(mapApiItemToUi);
      message.success(`找到 ${res.data.data.total} 个资源`);
      refreshUserInfo(); // 搜索后刷新配额
    }
  } catch (error) { message.error('搜索失败: ' + (error.response?.data?.message || error.message)); } finally { searching.value = false; }
};

const loadPresets = async () => {
  try {
    const res = await axios.get('/api/nullbr/presets');
    presetLists.value = res.data;
    presetMenuOptions.value = res.data.map(list => ({
        label: () => h(NTooltip, { placement: 'right', keepAliveOnHover: false }, { trigger: () => h('span', null, list.name), default: () => list.name }),
        key: list.id,
        icon: () => h(NIcon, null, { default: () => h(ListIcon) })
    }));
    if (presetLists.value.length > 0) handleListChange(presetLists.value[0].id);
  } catch (e) { message.error('加载片单列表失败'); }
};

const handleListChange = async (key) => {
  currentListId.value = key;
  listPage.value = 1;
  listItems.value = [];
  hasMore.value = true;
  await fetchListContent();
};

const loadMoreList = async () => {
    listPage.value++;
    loadingMore.value = true;
    await fetchListContent();
    loadingMore.value = false;
}

const fetchListContent = async () => {
  if (listPage.value === 1) loadingList.value = true;
  try {
    const res = await axios.post('/api/nullbr/list', { list_id: currentListId.value, page: listPage.value });
    if (res.data && res.data.data && res.data.data.list) {
      const newItems = res.data.data.list.map(mapApiItemToUi);
      if (newItems.length === 0) hasMore.value = false;
      else listItems.value.push(...newItems);
      refreshUserInfo(); // 刷新配额
    }
  } catch (error) { message.error('获取片单内容失败'); } finally { loadingList.value = false; }
};

const mapApiItemToUi = (item) => ({
  id: item.tmdbid || item.id,
  tmdb_id: item.tmdbid || item.id,
  title: item.title || item.name,
  poster: item.poster, 
  media_type: item.media_type || 'movie',
  overview: item.overview,
  vote: item.vote || item.vote_average,
  year: item.release_date ? item.release_date.substring(0, 4) : '',
  in_library: item.in_library,
  subscription_status: item.subscription_status
});

const nullbrModalRef = ref(null);
const loadingResourcesId = ref(null);

// ★★★ 新增：季选择相关状态 ★★★
const showSeasonModal = ref(false);
const loadingSeasons = ref(false);
const seasonList = ref([]);
const currentSeriesForSearch = ref(null);

// ★★★ 新增/修改：处理卡片点击，区分电影和剧集 ★★★
const handleResourceClick = async (item) => {
  // 1. 如果是电影，直接打开搜索
  if (item.media_type === 'movie') {
    if (nullbrModalRef.value) {
      nullbrModalRef.value.open(item);
    }
    return;
  }

  // 2. 如果是剧集，先弹出季选择框
  currentSeriesForSearch.value = item;
  showSeasonModal.value = true;
  loadingSeasons.value = true;
  seasonList.value = [];

  try {
    // 调用 TMDB 接口获取季信息 (确保后端存在 /api/discover/tmdb/tv/{id} 接口)
    // item.tmdb_id 是我们在 mapApiItemToUi 中映射的字段
    const res = await axios.get(`/api/discover/tmdb/tv/${item.tmdb_id}`);
    
    if (res.data && res.data.seasons) {
      // 过滤掉第0季(特别篇)，并按季号排序
      seasonList.value = res.data.seasons
        .filter(s => s.season_number > 0)
        .sort((a, b) => a.season_number - b.season_number);
    }
  } catch (e) {
    message.warning("获取季信息失败，将搜索整剧");
    // 如果获取失败，仍然保持 Modal 打开，让用户可以选择“搜索整剧”
    seasonList.value = [];
  } finally {
    loadingSeasons.value = false;
  }
};

// 分类规则相关状态
const sortingRules = ref([]);
const showRuleModal = ref(false);
const showRuleManagerModal = ref(false);
const currentRule = ref({});

// 选项数据
const rawMovieGenres = ref([]); // 电影类型原始数据
const rawTvGenres = ref([]);    // 剧集类型原始数据
const rawStudios = ref([]);     // 工作室原始数据 (带类型标记)

const countryOptions = ref([]); 
const languageOptions = ref([]);
const keywordOptions = ref([]);
const ratingOptions = ref([]);

// 1. 动态类型 (Genres) 选项
const computedGenreOptions = computed(() => {
  const type = currentRule.value.media_type; // 'all', 'movie', 'tv'
  
  if (type === 'movie') {
    return rawMovieGenres.value;
  } else if (type === 'tv') {
    return rawTvGenres.value;
  } else {
    // 'all': 合并去重
    const map = new Map();
    [...rawMovieGenres.value, ...rawTvGenres.value].forEach(g => map.set(g.value, g));
    return Array.from(map.values());
  }
});
const genreOptions = computed(() => {
  const map = new Map();
  // 合并电影和剧集的所有类型，利用 Map 去重 (ID相同的视为同一个)
  [...rawMovieGenres.value, ...rawTvGenres.value].forEach(g => {
    if (g && g.value) map.set(g.value, g);
  });
  return Array.from(map.values());
});
// 2. 动态工作室 (Studios) 选项
const computedStudioOptions = computed(() => {
  const type = currentRule.value.media_type;
  
  return rawStudios.value.filter(item => {
    if (type === 'all') return true;
    if (type === 'movie') return item.is_movie; // 仅显示有 company_ids 的
    if (type === 'tv') return item.is_tv;       // 仅显示有 network_ids 的
    return true;
  });
});

// 加载规则
const loadSortingRules = async () => {
  try {
    const res = await axios.get('/api/nullbr/sorting_rules');
    let data = res.data;
    
    //以此为防线：如果 axios 自动解析没生效（极少见），或者后端返回了双重序列化的字符串
    if (typeof data === 'string') {
        try { data = JSON.parse(data); } catch(e) {}
    }
    
    if (Array.isArray(data)) {
        sortingRules.value = data;
    } else {
        sortingRules.value = [];
    }
  } catch (e) {
      console.error("加载规则失败", e);
      sortingRules.value = [];
  }
};

// 保存规则
const saveSortingRules = async () => {
  try {
    await axios.post('/api/nullbr/sorting_rules', sortingRules.value);
  } catch (e) { message.error('保存规则失败'); }
};

const addRule = () => {
  currentRule.value = { 
    id: Date.now(), name: '', cid: '', enabled: true, 
    media_type: 'all', genres: [], countries: [], languages: [], 
    studios: [], keywords: [], ratings: [],
    year_min: null, year_max: null, runtime_min: null, runtime_max: null, min_rating: 0
  };
  showRuleModal.value = true;
};

const editRule = (rule) => {
  currentRule.value = JSON.parse(JSON.stringify(rule));
  showRuleModal.value = true;
};

const deleteRule = (rule) => {
  sortingRules.value = sortingRules.value.filter(r => r.id !== rule.id);
  saveSortingRules();
};

const confirmSaveRule = () => {
  if (!currentRule.value.name || !currentRule.value.cid) {
    message.error('名称和 CID 必填');
    return;
  }
  const idx = sortingRules.value.findIndex(r => r.id === currentRule.value.id);
  if (idx > -1) sortingRules.value[idx] = currentRule.value;
  else sortingRules.value.push(currentRule.value);
  
  saveSortingRules();
  showRuleModal.value = false;
};

const getRuleSummary = (rule) => {
  const parts = [];
  if (rule.media_type !== 'all') parts.push(rule.media_type === 'tv' ? '剧集' : '电影');
  
  // A. 直接显示中文的字段 (自定义集合)
  if (rule.studios?.length) parts.push(`工作室:${rule.studios.join(',')}`);
  if (rule.keywords?.length) parts.push(`关键词:${rule.keywords.join(',')}`);
  if (rule.ratings?.length) parts.push(`分级:${rule.ratings.join(',')}`);

  // B. 需要反查 Label 的字段 (存储的是 ID/Code)
  
  // 类型 (ID -> 中文)
  if (rule.genres?.length) {
      const names = rule.genres.map(id => {
          // 注意：id 可能是数字或字符串，做个兼容比较
          const opt = genreOptions.value.find(o => o.value == id);
          return opt ? opt.label : id;
      });
      parts.push(`类型:${names.join(',')}`);
  }
  
  // 国家 (Code -> 中文)
  if (rule.countries?.length) {
      const names = rule.countries.map(code => {
          const opt = countryOptions.value.find(o => o.value === code);
          return opt ? opt.label : code;
      });
      parts.push(`国家:${names.join(',')}`);
  }
  
  // 语言 (Code -> 中文)
  if (rule.languages?.length) {
      const names = rule.languages.map(code => {
          const opt = languageOptions.value.find(o => o.value === code);
          return opt ? opt.label : code;
      });
      parts.push(`语言:${names.join(',')}`);
  }
  
  // 年份范围
  if (rule.year_min || rule.year_max) {
      if (rule.year_min && rule.year_max) {
          parts.push(`年份:${rule.year_min}-${rule.year_max}`);
      } else if (rule.year_min) {
          parts.push(`年份:≥${rule.year_min}`);
      } else if (rule.year_max) {
          parts.push(`年份:≤${rule.year_max}`);
      }
  }

  // 时长范围 
  if (rule.runtime_min || rule.runtime_max) {
      if (rule.runtime_min && rule.runtime_max) {
          parts.push(`时长:${rule.runtime_min}-${rule.runtime_max}分`);
      } else if (rule.runtime_min) {
          parts.push(`时长:≥${rule.runtime_min}分`);
      } else if (rule.runtime_max) {
          parts.push(`时长:≤${rule.runtime_max}分`);
      }
  }

  // 最低评分
  if (rule.min_rating > 0) {
      parts.push(`评分:≥${rule.min_rating}`);
  }

  return parts.join(' + ') || '无条件';
};

// ★★★ 新增：选中季后触发搜索 ★★★
const selectSeasonAndSearch = (season) => {
  showSeasonModal.value = false;
  
  if (!currentSeriesForSearch.value) return;
  
  if (nullbrModalRef.value) {
    // 构造一个新的对象传给搜索组件，带上 season_number
    const searchItem = {
      ...currentSeriesForSearch.value,
      // ★ 关键：如果选了季，传入 season_number；没选(null)则不传
      season_number: season ? season.season_number : null 
    };
    
    nullbrModalRef.value.open(searchItem);
  }
};

// --- 115 目录浏览通用逻辑 ---
const showFolderPopover = ref(false); // 控制 Popover 显示
const loadingFolders = ref(false);
const folderList = ref([]);
const currentBrowserCid = ref('0');
const currentBrowserFolderName = ref('根目录');
const newFolderName = ref(''); // 新建文件夹名称
const showCreateFolderInput = ref(false); // 控制新建输入框显示
const selectorContext = ref({ type: '', key: '' });

// 打开选择器
const openFolderSelector = (type, initialCid = '0') => {
  selectorContext.value.type = type;
  showFolderPopover.value = true;
  // 如果传入了有效的 CID，尝试加载该目录，否则加载根目录
  const targetCid = (initialCid && initialCid !== '0') ? initialCid : '0';
  load115Folders(targetCid);
};

// 打开 Popover 时初始化
const handleFolderPopoverShow = (show) => {
  if (show) {
    // 如果当前输入框有值，尝试加载该值（如果后端支持反查路径最好，不支持就回根目录）
    // 这里简单起见，每次打开都回根目录，或者停留在上次浏览的位置
    if (currentBrowserCid.value === '0') {
        load115Folders('0');
    }
  }
};

// 加载目录 (保持不变，确保 API 路径正确)
const load115Folders = async (cid, folderName = null) => {
  loadingFolders.value = true;
  try {
    const res = await axios.get('/api/nullbr/115/dirs', { params: { cid } });
    if (res.data && res.data.success) {
      folderList.value = res.data.data;
      currentBrowserCid.value = cid;
      if (folderName) currentBrowserFolderName.value = folderName;
      if (cid === '0') currentBrowserFolderName.value = '根目录';
    }
  } catch (e) {
    message.error("加载目录失败: " + (e.response?.data?.message || e.message));
  } finally {
    loadingFolders.value = false;
  }
};

// 新建文件夹
const handleCreateFolder = async () => {
  if (!newFolderName.value) return;
  try {
    const res = await axios.post('/api/nullbr/115/mkdir', {
      pid: currentBrowserCid.value,
      name: newFolderName.value
    });
    if (res.data && res.data.status === 'success') {
      message.success('创建成功');
      newFolderName.value = '';
      showCreateFolderInput.value = false;
      // 刷新当前列表
      load115Folders(currentBrowserCid.value, currentBrowserFolderName.value);
    } else {
      message.error(res.data.message || '创建失败');
    }
  } catch (e) {
    message.error("请求失败: " + e.message);
  }
};

// 确认选择
const confirmFolderSelection = () => {
  const cid = currentBrowserCid.value;
  // 根目录显示 "/"，其他显示文件夹名
  const name = cid === '0' ? '/' : currentBrowserFolderName.value; 
  
  if (selectorContext.value.type === 'config') {
    config.p115_save_path_cid = cid;
    config.p115_save_path_name = name; // <--- 核心修改：保存名称用于显示
    message.success(`已选择目录: ${name}`);
  } else if (selectorContext.value.type === 'rule') {
    currentRule.value.cid = cid;
    currentRule.value.dir_name = name;
  }
  
  showFolderPopover.value = false;
};

const MediaCard = defineComponent({
  props: ['item', 'loading'],
  components: { NImage, NEllipsis, NSpace, NTag, NText, NSpin, NIcon },
  template: `
    <div class="media-card" @mouseenter="hover=true" @mouseleave="hover=false">
      <div v-if="loading" class="loading-overlay"><n-spin size="medium" stroke="#ffffff" /></div>
      <div class="poster-wrapper">
        <img :src="item.poster ? 'https://image.tmdb.org/t/p/w300' + item.poster : '/default-poster.png'" class="media-poster" loading="lazy"/>
        <div v-if="item.in_library" class="ribbon ribbon-green"><span>已入库</span></div>
        <div v-else-if="item.subscription_status === 'SUBSCRIBED'" class="ribbon ribbon-blue"><span>已订阅</span></div>
        <div v-if="item.vote" class="rating-badge">{{ Number(item.vote).toFixed(1) }}</div>
        <div class="overlay-info">
          <div class="text-content">
            <div class="media-title" :title="item.title">{{ item.title }}</div>
            <div class="media-meta-row"><span class="media-year">{{ item.year }}</span><span class="media-dot">·</span><span class="media-type">{{ item.media_type === 'tv' ? '剧集' : '电影' }}</span></div>
          </div>
        </div>
      </div>
    </div>
  `,
  data() { return { hover: false } }
});

// 购买兑换码相关逻辑 
const showBuyModal = ref(false);
const selectedPlanName = ref(null);
const selectedPlanPrice = ref(0);

const selectPlan = (name, price) => {
  selectedPlanName.value = name;
  selectedPlanPrice.value = price;
};

onMounted(async () => {
    loadConfig();
    loadPresets();
    loadSortingRules();
    
    try {
        // ★★★ 修改：分别获取并存储电影和剧集的类型 ★★★
        const [mGenres, tGenres] = await Promise.all([
             axios.get('/api/custom_collections/config/tmdb_movie_genres'),
             axios.get('/api/custom_collections/config/tmdb_tv_genres')
        ]);
        
        // 存储电影类型
        rawMovieGenres.value = (mGenres.data || []).map(g => ({ label: g.name, value: g.id }));
        // 存储剧集类型
        rawTvGenres.value = (tGenres.data || []).map(g => ({ label: g.name, value: g.id }));

        // ★★★ 修改：处理工作室数据，标记属于电影还是剧集 ★★★
        const sRes = await axios.get('/api/custom_collections/config/studios');
        // 后端返回结构: { label: "Netflix", value: "Netflix", types: ["movie", "tv"], ... }
        rawStudios.value = (sRes.data || []).map(s => {
            const types = s.types || []; // 获取后端返回的类型数组
            return {
                label: s.label,
                value: s.value, 
                // 只要数组里包含 'movie' 即视为支持电影
                is_movie: types.includes('movie'),
                // 只要数组里包含 'tv' 即视为支持剧集
                is_tv: types.includes('tv')
            };
        });

        const cRes = await axios.get('/api/custom_collections/config/tmdb_countries');
        countryOptions.value = cRes.data;
        
        const lRes = await axios.get('/api/custom_collections/config/languages');
        languageOptions.value = lRes.data;
        
        const kRes = await axios.get('/api/custom_collections/config/keywords');
        keywordOptions.value = kRes.data;
        
        const rRes = await axios.get('/api/custom_collections/config/unified_ratings_options');
        ratingOptions.value = (rRes.data || []).map(r => ({ label: r, value: r }));

    } catch (e) {
        console.error("加载选项失败", e);
    }
});
</script>

<style scoped>
/* 样式保持不变 */
.dashboard-card { height: 100%; }
.responsive-grid { display: grid; gap: 16px; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); }
.grid-item { min-width: 0; height: 100%; }
:deep(.media-card) { cursor: pointer; transition: transform 0.2s ease, box-shadow 0.2s ease; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); overflow: hidden; height: 100%; background-color: #222; display: flex; flex-direction: column; position: relative; }
:deep(.media-card:hover) { transform: translateY(-4px); box-shadow: 0 8px 16px rgba(0,0,0,0.3); z-index: 10; }
:deep(.poster-wrapper) { position: relative; width: 100%; aspect-ratio: 2 / 3; overflow: hidden; }
:deep(.media-poster) { width: 100%; height: 100%; object-fit: cover; display: block; transition: transform 0.3s ease; }
:deep(.media-card:hover .media-poster) { transform: scale(1.05); }
:deep(.loading-overlay) { position: absolute; top: 0; left: 0; right: 0; bottom: 0; z-index: 20; background: rgba(0,0,0,0.4); display: flex; align-items: center; justify-content: center; border-radius: 4px; }
:deep(.overlay-info) { position: absolute; bottom: 0; left: 0; right: 0; background: linear-gradient(to top, rgba(0,0,0,0.9) 0%, rgba(0,0,0,0.6) 50%, transparent 100%); padding: 40px 8px 8px 8px; display: flex; justify-content: space-between; align-items: flex-end; pointer-events: none; }
:deep(.text-content) { flex: 1; min-width: 0; }
:deep(.media-title) { color: #fff; font-weight: bold; font-size: 0.9em; line-height: 1.2; margin-bottom: 2px; text-shadow: 0 1px 2px rgba(0,0,0,0.8); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
:deep(.media-meta-row) { display: flex; align-items: center; color: rgba(255, 255, 255, 0.85); font-size: 0.75em; text-shadow: 0 1px 2px rgba(0,0,0,0.8); }
:deep(.media-dot) { margin: 0 4px; }
:deep(.rating-badge) { position: absolute; top: 6px; right: 6px; background-color: rgba(0, 0, 0, 0.65); color: #f7b824; padding: 2px 5px; border-radius: 4px; font-size: 10px; font-weight: bold; backdrop-filter: blur(2px); box-shadow: 0 1px 2px rgba(0,0,0,0.3); z-index: 5; }
:deep(.ribbon) { position: absolute; top: -3px; left: -3px; width: 60px; height: 60px; overflow: hidden; z-index: 5; }
:deep(.ribbon span) { position: absolute; display: block; width: 85px; padding: 3px 0; box-shadow: 0 2px 4px rgba(0,0,0,0.2); color: #fff; font-size: 10px; font-weight: bold; text-shadow: 0 1px 1px rgba(0,0,0,0.3); text-transform: uppercase; text-align: center; left: -16px; top: 10px; transform: rotate(-45deg); }
:deep(.ribbon-green span) { background-color: #67c23a; }
:deep(.ribbon-blue span) { background-color: #409eff; }

.section-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 16px;
  font-weight: 600;
  margin-bottom: 16px;
  color: var(--n-text-color);
  border-left: 4px solid var(--n-primary-color);
  padding-left: 10px;
}

.sub-module {
  background-color: rgba(128, 128, 128, 0.05); /* 半透明背景，深浅通用 */
  border: 1px solid rgba(128, 128, 128, 0.1);
  border-radius: 8px;
  padding: 16px;
  margin-top: 16px;
}

.organize-summary-box {
  background-color: rgba(255, 255, 255, 0.05); /* 深色模式下微亮，浅色模式下微灰 */
  border: 1px solid rgba(128, 128, 128, 0.1);
  border-radius: 4px;
  padding: 12px;
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.sub-module-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}

.sub-module-header .title {
  font-weight: bold;
  font-size: 14px;
}

.filter-box { padding: 0 4px; }

.preset-container {
  background-color: rgba(128, 128, 128, 0.03);
  border-radius: 8px;
  padding: 12px;
  max-height: 600px;
  overflow-y: auto;
  border: 1px solid rgba(128, 128, 128, 0.1);
}

.preset-item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
}

/* 用户卡片样式 */
.user-card {
  background: linear-gradient(135deg, rgba(24, 160, 88, 0.1) 0%, rgba(32, 128, 240, 0.1) 100%);
  border: 1px solid rgba(128, 128, 128, 0.1);
  border-radius: 8px;
  padding: 16px;
  margin-bottom: 16px;
}
.user-card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}
.user-level {
  display: flex;
  align-items: center;
  gap: 8px;
}
.expire-date {
  font-size: 12px;
  color: #666;
}
.quota-info {
  font-size: 12px;
}
.quota-row {
  display: flex;
  justify-content: space-between;
  margin-bottom: 4px;
  color: #555;
}
/* ★★★ 新增：定价表样式 ★★★ */
.pricing-container {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
  gap: 20px;
  margin-top: 20px;
}

.pricing-card {
  background: #252525;
  border-radius: 12px;
  padding: 24px;
  text-align: center;
  border: 1px solid #333;
  position: relative;
  transition: transform 0.3s;
}

.pricing-card:hover {
  transform: translateY(-5px);
  border-color: #555;
}

.pricing-card.silver { border-top: 4px solid #409eff; }
.pricing-card.golden { border-top: 4px solid #e6a23c; }

.plan-badge {
  position: absolute;
  top: 0;
  right: 0;
  background: #409eff;
  color: white;
  font-size: 12px;
  padding: 2px 8px;
  border-bottom-left-radius: 8px;
  border-top-right-radius: 8px;
}

.plan-name {
  font-size: 18px;
  font-weight: bold;
  color: #fff;
  margin-bottom: 10px;
}

.plan-price {
  font-size: 32px;
  font-weight: bold;
  color: #fff;
  margin-bottom: 20px;
}

.plan-price .period {
  font-size: 14px;
  color: #999;
  font-weight: normal;
}

.plan-features {
  text-align: left;
  margin-bottom: 24px;
  font-size: 13px;
  color: #ccc;
}

.feature {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.feature.disabled {
  color: #666;
  text-decoration: line-through;
}

/* 支付弹层样式 */
.payment-box {
  display: flex;
  flex-wrap: wrap;
  gap: 30px;
  background: #2c2c2c;
  padding: 20px;
  border-radius: 8px;
}

.payment-left {
  flex: 1;
  text-align: center;
  min-width: 200px;
  border-right: 1px solid #444;
}

.payment-right {
  flex: 1.5;
  display: flex;
  flex-direction: column;
  justify-content: center;
  min-width: 250px;
}

.pay-title { font-size: 18px; font-weight: bold; margin-bottom: 5px; }
.pay-desc { font-size: 12px; color: #999; margin-bottom: 15px; }
.qr-code { width: 180px; height: 180px; border-radius: 8px; }

.step-item {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 15px;
}

.step-num {
  width: 24px;
  height: 24px;
  background: #409eff;
  color: white;
  border-radius: 50%;
  text-align: center;
  line-height: 24px;
  font-size: 12px;
  font-weight: bold;
}

.step-text { font-size: 14px; color: #ddd; }
.rules-container {
  background: transparent;
  border: none;
  padding: 0;
}
.rule-item {
  display: flex;
  align-items: center;
  background-color: rgba(255, 255, 255, 0.02); /* 极淡的背景 */
  border: 1px solid rgba(128, 128, 128, 0.1);
  padding: 12px;
  margin-bottom: 8px;
  border-radius: 6px;
  transition: all 0.2s;
}
.rule-item:hover {
  background-color: rgba(255, 255, 255, 0.05);
  border-color: var(--n-primary-color);
}
.drag-handle { 
  cursor: grab; 
  color: #999; 
  margin-right: 12px; 
  padding: 4px;
}
.drag-handle:active { cursor: grabbing; }
.rule-info { flex: 1; }
.rule-name { font-weight: bold; font-size: 13px; }
.rule-actions { display: flex; align-items: center; gap: 4px; }
/* 目录浏览器样式优化 - 全面适配 Naive UI 主题变量 */
.folder-browser {
  display: flex;
  flex-direction: column;
  height: 500px;
  /* 关键：使用 Naive UI 的模态框背景色变量，而不是写死颜色 */
  background-color: var(--n-color-modal); 
  color: var(--n-text-color);
  border-radius: 4px;
  overflow: hidden;
  border: 1px solid var(--n-divider-color);
  transition: background-color 0.3s, color 0.3s;
}

.browser-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 16px;
  border-bottom: 1px solid var(--n-divider-color);
  /* 头部使用稍微不同的背景色 (action-color 通常是浅灰或深灰) */
  background-color: var(--n-action-color); 
  flex-shrink: 0;
}

.nav-left {
  display: flex;
  align-items: center;
  flex: 1;
  overflow: hidden;
}

.breadcrumbs {
  flex: 1;
  font-size: 13px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  margin-left: 8px;
  display: flex;
  align-items: center;
  color: var(--n-text-color-3); /* 三级文字颜色 */
}

.crumb-item { 
  cursor: pointer; 
  transition: color 0.2s; 
}
.crumb-item:hover { 
  color: var(--n-primary-color); 
}
.crumb-item.current { 
  color: var(--n-text-color-1); /* 主要文字颜色 */
  font-weight: 600; 
  cursor: default; 
}
.separator { 
  margin: 0 6px; 
  color: var(--n-text-color-disabled); 
}

/* 列表区域 */
.folder-list-container {
  flex: 1;
  overflow-y: auto;
  position: relative;
  background-color: transparent; /* 透明背景，透出外层的 modal 颜色 */
}

.folder-list {
  padding: 4px 0;
}

.folder-item {
  display: flex;
  align-items: center;
  padding: 10px 16px;
  cursor: pointer;
  transition: background-color 0.2s;
  border-bottom: 1px solid var(--n-divider-color);
  color: var(--n-text-color-2);
}

/* 悬停效果：使用 Naive UI 定义的 hover 颜色 */
.folder-item:hover {
  background-color: var(--n-hover-color); 
}

.folder-icon-wrapper {
  display: flex;
  align-items: center;
  margin-right: 12px;
}

.folder-name {
  flex: 1;
  font-size: 14px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  color: var(--n-text-color-1); /* 强制使用主要文字颜色 */
}

/* 底部栏 */
.browser-footer {
  padding: 12px 16px;
  border-top: 1px solid var(--n-divider-color);
  display: flex;
  justify-content: space-between;
  align-items: center;
  background-color: var(--n-color-modal);
  flex-shrink: 0;
}

.current-info span {
    color: var(--n-text-color-3);
    font-size: 12px;
}

/* 规则列表项的样式修复 (确保在亮色模式下也能看清) */
.rule-item {
  display: flex;
  align-items: center;
  /* 使用 action-color 作为背景，自动适配明暗 */
  background-color: var(--n-action-color); 
  border: 1px solid var(--n-divider-color);
  padding: 12px;
  margin-bottom: 8px;
  border-radius: 6px;
  transition: all 0.2s;
}

.rule-item:hover {
  border-color: var(--n-primary-color);
  background-color: var(--n-hover-color);
}

.rule-name {
  color: var(--n-text-color-1);
  font-weight: bold;
  font-size: 13px;
}

.rule-desc span {
  color: var(--n-text-color-3);
}
@media (max-width: 600px) {
  .hide-on-mobile {
    display: none;
  }
}
</style>