#ifndef MELEE_GM_GMTITLEMODE_H
#define MELEE_GM_GMTITLEMODE_H

#include "melee/gm/types.h"

typedef struct TitleExitData {
    u32 buttons;
    u32 x4;
} TitleExitData;
ASSERT_SIZE(TitleExitData, 0x8);

/* 1B087C */ void gmTitleMode_OnEnter(GameModeState*);
/* 3DD6A0 */ extern GameModeState gm_Mode_Title_States[];

#endif
